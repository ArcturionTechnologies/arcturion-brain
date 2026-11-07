"""Hermetic test: raw session-transcript logs are down-ranked below authored knowledge,
and near-duplicate restatements (same content, titles differing only by date/casing) collapse
to one in the query pack.

Why (braintune 2026-06-28): wiki/logs/sessions/ transcripts are verbatim echoes of the
user's prompt, so they pack every query term into their title and won the term-overlap
signal in _relevance_score — a topic like "NEMESIS FDCPA credit dispute" returned a pack
of 6 transcript twins and ZERO authored doctrine. Two guards fix it:
  1. _SESSION_LOG_PENALTY demotes any node whose path is under logs/sessions/.
  2. _content_fingerprint collapses restatements whose titles differ (date prefix, casing)
     so title-dedup misses them.

Run:  python3 -m engine.tests.test_session_log_demotion
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .. import config as C
from .. import db as DB


def _n(nid, title, path, summary, status="active"):
    return {"id": nid, "type": "Source", "family": "document", "title": title,
            "path": path, "owner": "NEMESIS", "status": status, "governance_tier": 0,
            "content_hash": None, "summary": summary, "body_excerpt": None,
            "centrality": 0.0, "created": None,
            "last_modified": "2026-06-16T00:00:00Z", "last_reviewed": None, "metadata": {}}


def run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="brain_sesslog_test_"))
    orig_db = C.BRAIN_DB
    C.BRAIN_DB = tmp / "brain.sqlite"
    try:
        from .. import brain

        conn = DB.connect(C.BRAIN_DB)
        DB.init_db(conn)

        SUMM = ("## User Requests 1. NEMESIS, YOU ARE MY ELITE CREDIT REPAIR ATTORNEY "
                "in this session, parse the dispute response and save it.")
        # 3 near-duplicate session transcripts: titles differ only by DATE + CASING, all
        # under wiki/logs/sessions/. These restate the SAME content.
        DB.upsert_node(conn, _n("sess_a",
            "Session 2026-05-25 — Nemesis, in this session you are my ELITE CREDIT REPAIR ATTORNEY",
            "11_INFRASTRUCTURE/1118_SHARED/wiki/logs/sessions/2026-05-25-aaa.md", SUMM))
        DB.upsert_node(conn, _n("sess_b",
            "Session 2026-05-26 — NEMESIS, YOU ARE MY ELITE CREDIT REPAIR ATTORNEY",
            "11_INFRASTRUCTURE/1118_SHARED/wiki/logs/sessions/2026-05-26-bbb.md", SUMM))
        DB.upsert_node(conn, _n("sess_c",
            "Session 2026-05-27 — Nemesis, in this session you are my ELITE CREDIT REPAIR ATTORNEY",
            "11_INFRASTRUCTURE/1118_SHARED/wiki/logs/sessions/2026-05-27-ccc.md", SUMM))
        # Authored knowledge — the doctrine that should WIN the pack.
        DB.upsert_node(conn, _n("doctrine",
            "Credit Law (FCRA/FDCPA) — Knowledge Base",
            "NEMESIS/000 Nemesis/Knowledge/credit-law-fcra-fdcpa.md",
            "Controlling authorities for credit dispute work: FCRA and FDCPA, debt validation."))
        conn.commit()

        pack = brain.query("NEMESIS FDCPA credit dispute attorney", token_budget=2000)
        ids = [n["id"] for n in pack.nodes]

        # (a) the authored doctrine outranks every session transcript
        assert "doctrine" in ids, f"doctrine node missing from pack: {ids}"
        assert ids[0] == "doctrine", f"doctrine should rank #1, got order: {ids}"

        # (b) the near-duplicate session restatements collapse to AT MOST one
        sess_kept = [i for i in ids if i.startswith("sess_")]
        assert len(sess_kept) <= 1, f"near-dup session restatements not collapsed: {sess_kept}"

        # (c) the fingerprint helper actually treats the three as one
        fps = {brain._content_fingerprint(_n(x, t, p, SUMM))
               for x, t, p in [("sess_a", "Session 2026-05-25 — A",
                                "x/wiki/logs/sessions/a.md"),
                               ("sess_b", "Session 2026-05-26 — B",
                                "x/wiki/logs/sessions/b.md")]}
        assert len(fps) == 1, f"date/casing twins did not share a fingerprint: {fps}"

        conn.close()
        print("PASS — session transcripts demoted below authored doctrine; "
              "date/casing restatements collapse to one")
        return 0
    finally:
        C.BRAIN_DB = orig_db
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(run())
