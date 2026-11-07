"""Answer-quality ranking invariants against a real (hermetic) graph — no pytest.

This is the ONE test that drives `brain.query` end-to-end on a DB it built itself, so it
guards the ranking invariant the live golden set cannot: given a canonical node and a
tempting twin that is an archive copy or `status=superseded`, the canonical one must win
the top slot.

Rewritten 2026-08-04 (Leg 1, retrieval-honesty). It previously asserted through
`engine.eval.answer_quality`, whose `_on_topic` check was a substring match on
title+summary — a check the twins passed too, since both carry the same words. The
assertion "the archive copy did not win" really meant "some node containing the word
'revenue' won", which the archive copy satisfies. It now asserts node IDs.

Run:
    python3 -m engine.tests.test_brain_answer_quality          # hermetic invariants
    python3 -m engine.tests.test_brain_answer_quality --live   # live golden-set readout
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .. import config as C
from .. import db as DB


def _n(nid, title, path, summary, status="active"):
    return {"id": nid, "type": "Source", "family": "document", "title": title,
            "path": path, "owner": "X", "status": status, "governance_tier": 0,
            "content_hash": None, "summary": summary, "body_excerpt": None,
            "centrality": 0.0, "created": None, "last_modified": "2026-06-16", "metadata": {}}


def run_hermetic() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="brain_aq_test_"))
    orig_db = C.BRAIN_DB
    C.BRAIN_DB = tmp / "brain.sqlite"
    try:
        from .. import retrieval_eval as E

        conn = DB.connect(C.BRAIN_DB)
        DB.init_db(conn)
        # Each pair: a canonical live node + a tempting twin with IDENTICAL words that must
        # not take the top slot. Identical text is the point — it is exactly what defeats a
        # substring-based scorer.
        DB.upsert_node(conn, _n("rev_live", "Quarterly Revenue", "SIRIUS/reports/rev.md",
                                "quarterly revenue numbers"))
        DB.upsert_node(conn, _n("rev_arch", "Quarterly Revenue", "SIRIUS/99_ARCHIVE/rev.md",
                                "quarterly revenue numbers"))
        DB.upsert_node(conn, _n("plan_live", "Launch Plan", "IO/launch/plan.md",
                                "go to market launch plan"))
        DB.upsert_node(conn, _n("plan_super", "Launch Plan", "IO/old/plan.md",
                                "go to market launch plan", status="superseded"))
        conn.commit()
        conn.close()

        # `unexpected` makes the twin an explicit failure rather than something merely
        # absent from `expect`: a node that should not be in the top 5 fails the entry even
        # when the right answer is #1.
        entries = [
            {"id": "inv-01", "class": "recency", "q": "quarterly revenue report",
             "expect": ["rev_live"], "unexpected": ["rev_arch"],
             "note": "archive copy of an identical document must not outrank the live one"},
            {"id": "inv-02", "class": "recency", "q": "go to market launch plan",
             "expect": ["plan_live"], "unexpected": ["plan_super"],
             "note": "status=superseded twin must not outrank the live one"},
        ]
        result = E.run(entries, token_budget=1500)

        detail = {d["id"]: d for d in result["detail"]}
        assert result["by_class"]["recency"]["pass_rate"] == 1.0, \
            f"a ranking invariant broke: {result['detail']}"
        assert result["positive"]["top1_rate"] == 1.0, \
            f"canonical node lost the top slot: {result['detail']}"
        for eid in ("inv-01", "inv-02"):
            assert not detail[eid]["superseded_hits"], \
                f"{eid}: non-canonical twin surfaced in the top 5: {detail[eid]}"

        # is_canonical unit checks (unchanged semantics, moved module)
        assert E.is_canonical({"status": "active", "path": "X/a.md"})
        assert not E.is_canonical({"status": "superseded", "path": "X/a.md"})
        assert not E.is_canonical({"status": "active", "path": "X/99_ARCHIVE/a.md"})

        print(f"PASS — answer-quality invariants: top1={result['positive']['top1_rate']} "
              f"recall@5={result['positive']['recall5_rate']} "
              f"(archive/superseded twins never surfaced)")
        return 0
    finally:
        C.BRAIN_DB = orig_db
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def run_live() -> int:
    """Readout only — the gate is `python3 -m engine.retrieval_eval`, which compares against
    the pinned baseline and alerts. This prints the same numbers without the verdict."""
    from .. import retrieval_eval as E
    r = E.run(verbose=True)
    print(f"\nLIVE golden set: recall@5={r['positive']['recall5_rate']} "
          f"top1={r['positive']['top1_rate']} guards={r['guards']['pass_rate']} "
          f"({r['questions']} questions)")
    return 0


if __name__ == "__main__":
    if "--live" in sys.argv:
        sys.exit(run_live())
    sys.exit(run_hermetic())
