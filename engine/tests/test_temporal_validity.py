"""Hermetic unit tests for temporal validity (Leg 4, retrieval-honesty 2026-08-06). No
pytest, mirrors test_retrieval_v2.py's fixture-DB pattern.

An additive `-12` score penalty (`_authority_score`'s `superseded_by` check) is just a
number competing with other numbers — there is always a fusion+recency+similarity
combination that beats it, so a stale document can still win a recency question. This leg's
fix is a validity WINDOW: `nodes.valid_until`, stamped ONLY by `engine.maintenance
.validity_pass` from an explicit supersession/contradiction record, sorts a node into a
tier BELOW every live node (`brain.rank_key_v2`) regardless of score — while never removing
it from the candidate set.

The fixture pair mirrors the REAL GnuCash → CSV-reader supersession (Memory decision
`arcfin-gnucash-to-csv-reader-migration-2026-07-05`, golden question `rec-01`, and the
`supersession_20260806_gnucash_to_csv_reader` record appended to the live
`index/supersessions.jsonl` for this leg) — same ids, same titles, same dates — built as an
isolated temp DB + temp jsonl so the test suite stays hermetic and does not depend on the
live index's future contents.

Covers:
  1. stamp_from_supersessions — marks the OLD id(s), never the NEW id, from an explicit
     old_id/new_id record; skips records with no ids / wrong status / unresolved ids.
  2. stamp_from_contradictions — marks the invalidated id from a resolved contradiction.
  3. rank_key_v2 tier — an expired node is PROVABLY ABSENT from top-5 for its successor's
     question, even when handed an artificial score advantage that would win under the old
     additive path.
  4. no-validity-data parity — nodes with no valid_until rank identically whether the flag
     is on or off (the additive tier changes nothing for the untouched majority of the
     graph).
  5. migration idempotency — running init_db twice does not error or duplicate columns.
  6. automatic same-subject inference emits a PROPOSAL only — valid_until is never written
     from the heuristic itself.

Run:  python3 -m engine.tests.test_temporal_validity
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from .. import config as C
from .. import db as DB


def _n(nid, title, path, summary, status="active", last_modified="2026-06-16T00:00:00",
       created=None, valid_until=None):
    return {"id": nid, "type": "Source", "family": "document", "title": title,
            "path": path, "owner": "SIRIUS", "status": status, "governance_tier": 0,
            "content_hash": None, "summary": summary, "body_excerpt": summary,
            "centrality": 0.0, "created": created, "last_modified": last_modified,
            "last_reviewed": None, "metadata": {}, "valid_until": valid_until}


# The real pair, ids and dates unchanged from the live index.
OLD_A = "mem_sqlite_format_gnucash_as_the_canonical_books_system_of_record_617003"
OLD_B = "mem_adr_gnucash_canonical_accounting_2026_06_07_c81db2"
NEW_ID = "mem_arcfin_gnucash_to_csv_reader_migration_2026_07_05_47e86c"
ACTIVATED = "2026-07-05T06:48:00"


def _gnucash_supersession_record():
    return {
        "id": "supersession_20260806_gnucash_to_csv_reader",
        "status": "active",
        "old_id": [OLD_A, OLD_B],
        "new_id": NEW_ID,
        "old": "GnuCash (SQLite-format) declared the canonical books system-of-record",
        "new": "GnuCash migration to a CSV-reader system-of-record; GnuCash retired",
        "created": "2026-07-05",
        "activated": ACTIVATED,
    }


def _fixture_env():
    """Isolated temp DB + temp index dir (supersessions/contradictions jsonl) + temp
    staging dir. Returns (tmp, conn) with C.BRAIN_DB / C.GRAPH_DIR / C.STAGING_DIR patched;
    caller must restore in a finally block."""
    tmp = Path(tempfile.mkdtemp(prefix="brain_temporal_validity_test_"))
    (tmp / "index").mkdir()
    (tmp / "staging" / "proposals").mkdir(parents=True)
    C.BRAIN_DB = tmp / "index" / "brain.sqlite"
    C.GRAPH_DIR = tmp / "index"
    C.STAGING_DIR = tmp / "staging" / "proposals"
    conn = DB.connect(C.BRAIN_DB)
    DB.init_db(conn)
    DB.upsert_node(conn, _n(OLD_A, "SQLite-format GnuCash as the canonical books system",
                            "SIRIUS/decisions/gnucash_a.md",
                            "GnuCash is the system of record for the books.",
                            created="2026-06-30", last_modified="2026-06-30T21:46:38"))
    DB.upsert_node(conn, _n(OLD_B, "ADR: GnuCash canonical accounting",
                            "SIRIUS/decisions/gnucash_b.md",
                            "GnuCash remains canonical for accounting.",
                            last_modified="2026-06-09T21:57:37"))
    DB.upsert_node(conn, _n(NEW_ID, "arcfin-gnucash-to-csv-reader-migration-2026-07-05",
                            "SIRIUS/decisions/csv_reader.md",
                            "GnuCash is retired. The CSV reader is the canonical books "
                            "system of record now.",
                            last_modified="2026-07-05T06:48:00"))
    DB.upsert_node(conn, _n("unrelated", "Dehumidifier wifi setup",
                            "ALTAIR/home/dehumidifier.md",
                            "How the dehumidifier joins the wifi network."))
    conn.commit()
    return tmp, conn


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# --------------------------------------------------------------- 1. supersessions.jsonl

def test_stamp_from_supersessions_marks_old_not_new() -> None:
    from ..maintenance import validity_pass as VP

    tmp, conn = _fixture_env()
    try:
        sup_path = C.GRAPH_DIR / "supersessions.jsonl"
        _write_jsonl(sup_path, [
            _gnucash_supersession_record(),
            {"id": "no_ids_here", "status": "active",
             "summary": "free-text governance log entry, no node ids"},
            {"id": "wrong_status", "status none-open": "x", "old_id": "ghost_a",
             "new_id": "ghost_b"},
            {"id": "unresolvable", "status": "active", "old_id": "does_not_exist",
             "new_id": NEW_ID, "activated": "2026-01-01"},
        ])

        result = VP.stamp_from_supersessions(conn, sup_path, verbose=False, apply=True)
        assert result["stamped"] == 2, result
        assert set(result["ids"]) == {OLD_A, OLD_B}, result["ids"]
        assert result["skipped_no_ids"] >= 1
        assert result["skipped_unresolved"] >= 1

        rows = {r["id"]: r["valid_until"] for r in
                conn.execute("SELECT id, valid_until FROM nodes").fetchall()}
        assert rows[OLD_A] == ACTIVATED, rows
        assert rows[OLD_B] == ACTIVATED, rows
        assert rows[NEW_ID] is None, "the NEW id must never be stamped"
        assert rows["unrelated"] is None

        # Idempotent — re-running applies the same stamp, does not error or double it.
        result2 = VP.stamp_from_supersessions(conn, sup_path, verbose=False, apply=True)
        assert result2["stamped"] == 2, result2
        rows2 = {r["id"]: r["valid_until"] for r in
                 conn.execute("SELECT id, valid_until FROM nodes").fetchall()}
        assert rows2 == rows

        # Dry run writes nothing.
        conn.execute("UPDATE nodes SET valid_until=NULL")
        conn.commit()
        dry = VP.stamp_from_supersessions(conn, sup_path, verbose=False, apply=False)
        assert dry["applied"] is False and dry["stamped"] == 0
        assert dry["candidates"] == 2
        still_null = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE valid_until IS NOT NULL").fetchone()[0]
        assert still_null == 0, "dry-run must not write"
        print("  ok  1. stamp_from_supersessions — marks OLD id(s) only, skips unresolved/"
              "no-id/wrong-status records, idempotent, dry-run writes nothing")
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------- 2. contradictions.jsonl

def test_stamp_from_contradictions_marks_invalidated() -> None:
    from ..maintenance import validity_pass as VP

    tmp, conn = _fixture_env()
    try:
        con_path = C.GRAPH_DIR / "contradictions.jsonl"
        _write_jsonl(con_path, [
            {"id": "c1", "status": "resolved", "invalidates_id": OLD_A,
             "resolved_at": "2026-07-06T00:00:00"},
            {"id": "c2", "status": "active", "invalidates_id": OLD_B,
             "resolved_at": "2026-07-06T00:00:00"},  # not yet resolved — must not apply
            {"id": "c3", "status": "resolved", "invalidates_id": "ghost",
             "resolved_at": "2026-07-06T00:00:00"},  # unresolved id
        ])
        result = VP.stamp_from_contradictions(conn, con_path, verbose=False, apply=True)
        assert result["stamped"] == 1, result
        assert result["ids"] == [OLD_A]
        row = conn.execute("SELECT valid_until FROM nodes WHERE id=?", (OLD_A,)).fetchone()
        assert row["valid_until"] == "2026-07-06T00:00:00"
        row_b = conn.execute("SELECT valid_until FROM nodes WHERE id=?", (OLD_B,)).fetchone()
        assert row_b["valid_until"] is None, "an unresolved (status=active) record must not apply"
        print("  ok  2. stamp_from_contradictions — marks only resolved+resolvable records")
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------- 3. rank tier + end-to-end

def test_expired_tier_beats_any_score_advantage() -> None:
    """The rank_key_v2 shape itself: (pin_tier, validity_tier, score). Mirrors
    test_true_exact_token_pin's method — construct the exact tuple the sort key produces
    and prove the tier order holds REGARDLESS of score, the same "regardless" argument the
    pin docstring makes, aimed at staleness instead of rarity."""
    from .. import brain as B

    now = B.datetime(2026, 8, 6, tzinfo=B.timezone.utc)
    expired = {"valid_until": "2026-07-05T06:48:00"}   # OLD_A/OLD_B's real stamp
    live_undated = {"valid_until": None}
    live_future = {"valid_until": "2099-01-01T00:00:00"}

    def key(node, score):
        return (0, B._validity_tier(node, now), score)

    # Hand the expired node an absurd score advantage — better than any real
    # fusion+recency+similarity combination could produce — and the tier must still lose.
    assert key(live_undated, 0.0) > key(expired, 10_000.0), \
        "an expired node beat a live one on score — that is the additive-penalty bug, back"
    assert key(live_future, 0.0) > key(expired, 10_000.0)
    # Two live nodes still order purely on score — the tier is additive-above, not a
    # reshuffle of the live population.
    assert key(live_undated, 5.0) < key(live_future, 6.0)
    print("  ok  3a. rank_key_v2 tier shape — expired loses to live REGARDLESS of score "
          "(undated and future-dated both count as live)")


def test_expired_node_ranks_below_every_live_decoy() -> None:
    """End to end through brain.query(): the real GnuCash pair, stamped by
    validity_pass from the actual supersessions.jsonl record, handed a fresher
    last_modified than its successor (the score advantage the old additive -12 penalty
    could lose to) — and STILL sorts after every live node that answers the question."""
    from .. import rank_signals as RS

    tmp, conn = _fixture_env()
    orig = (C.RETRIEVAL_V2, C.TEMPORAL_VALIDITY_TIER, C.VECTOR_SEED_ENABLED,
            C.RELEVANCE_FLOOR, C.PACK_PER_SOURCE_CAP)
    try:
        RS.reset_cache()
        C.RETRIEVAL_V2 = True
        C.TEMPORAL_VALIDITY_TIER = True
        C.VECTOR_SEED_ENABLED = False
        C.RELEVANCE_FLOOR = 0.0          # isolate the validity tier from the floor
        C.PACK_PER_SOURCE_CAP = 0        # isolate it from the per-folder demotion too

        sup_path = C.GRAPH_DIR / "supersessions.jsonl"
        _write_jsonl(sup_path, [_gnucash_supersession_record()])
        _write_jsonl(C.GRAPH_DIR / "contradictions.jsonl", [])

        from ..maintenance import validity_pass as VP
        VP.stamp_from_supersessions(conn, sup_path, verbose=False, apply=True)

        # Hand the now-expired node an unfair edge: fresher last_modified than its
        # successor (an artificial score advantage the OLD additive -12 penalty could
        # easily lose to). The tier must still sink it below any live node that matches.
        conn.execute("UPDATE nodes SET last_modified=? WHERE id=?",
                     ("2026-08-06T00:00:00", OLD_A))
        DB.upsert_node(conn, _n(
            "decoy_live", "books system of record reference note",
            "SIRIUS/decisions/decoy_live.md",
            "canonical books system of record — a live, un-superseded reference.",
            last_modified="2026-07-20T00:00:00"))
        conn.commit()

        from .. import brain as B
        pack = B.query("canonical books system of record", token_budget=4000, radius=1)
        ids = [n["id"] for n in pack.nodes]
        assert OLD_A in ids and "decoy_live" in ids, ids
        assert ids.index(OLD_A) > ids.index("decoy_live"), (
            f"expired node outranked a live one despite a fresher last_modified: {ids}")
        assert ids.index(NEW_ID) < ids.index(OLD_A), \
            f"the live successor did not outrank the expired predecessor: {ids}"

        # NEVER filtered — still retrievable, still THE answer when nothing live competes.
        pack2 = B.query("SQLite-format GnuCash canonical books system", token_budget=4000,
                        radius=0)
        assert any(n["id"] == OLD_A for n in pack2.nodes), \
            "expired node must remain retrievable, not deleted/hidden"
        print("  ok  3b. expired node — ranks below every live node despite a score "
              "advantage; still retrievable, never hidden, when it's the only answer")
    finally:
        conn.close()
        (C.RETRIEVAL_V2, C.TEMPORAL_VALIDITY_TIER, C.VECTOR_SEED_ENABLED,
         C.RELEVANCE_FLOOR, C.PACK_PER_SOURCE_CAP) = orig
        RS.reset_cache()
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------- 4. no-data parity

def test_no_validity_data_ranks_identically_flag_on_off() -> None:
    """Nodes carrying no valid_until must rank identically whether
    C.TEMPORAL_VALIDITY_TIER is on or off — the tier is additive, not a reshuffle of the
    untouched majority of the graph."""
    from .. import brain as B
    from .. import rank_signals as RS

    tmp, conn = _fixture_env()
    orig_v2, orig_tier, orig_vec = C.RETRIEVAL_V2, C.TEMPORAL_VALIDITY_TIER, C.VECTOR_SEED_ENABLED
    try:
        C.RETRIEVAL_V2 = True
        C.VECTOR_SEED_ENABLED = False
        _write_jsonl(C.GRAPH_DIR / "supersessions.jsonl", [])
        _write_jsonl(C.GRAPH_DIR / "contradictions.jsonl", [])

        RS.reset_cache()
        C.TEMPORAL_VALIDITY_TIER = False
        off = [n["id"] for n in B.query("canonical books system of record",
                                        token_budget=4000, radius=1).nodes]

        RS.reset_cache()
        C.TEMPORAL_VALIDITY_TIER = True
        on = [n["id"] for n in B.query("canonical books system of record",
                                       token_budget=4000, radius=1).nodes]

        assert off == on, f"no-data parity broken: off={off} on={on}"
        print("  ok  4. no-validity-data nodes — identical ranking with the flag on or off")
    finally:
        conn.close()
        C.RETRIEVAL_V2, C.TEMPORAL_VALIDITY_TIER = orig_v2, orig_tier
        C.VECTOR_SEED_ENABLED = orig_vec
        RS.reset_cache()
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------- 5. migration idempotency

def test_migration_idempotent() -> None:
    tmp, conn = _fixture_env()
    try:
        DB.init_db(conn)
        DB.init_db(conn)  # must not raise, must not duplicate columns
        cols = [r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()]
        assert cols.count("valid_from") == 1, cols
        assert cols.count("valid_until") == 1, cols

        # A pre-existing node from BEFORE this migration (no valid_from/valid_until in the
        # dict at all) behaves exactly as today: both columns NULL, no KeyError.
        conn.execute(
            "INSERT INTO nodes (id, type, family, title, status) VALUES "
            "('legacy_node', 'Source', 'document', 'legacy', 'active')")
        conn.commit()
        row = dict(conn.execute("SELECT * FROM nodes WHERE id='legacy_node'").fetchone())
        assert row["valid_from"] is None and row["valid_until"] is None
        print("  ok  5. migration — init_db idempotent, additive columns, legacy rows "
              "behave exactly as before")
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------- 6. proposal-only inference

def test_same_subject_inference_only_proposes_never_applies() -> None:
    from ..maintenance import validity_pass as VP

    tmp, conn = _fixture_env()
    try:
        # Two nodes, same normalized subject ("gnucash migration status"), different
        # last_modified by a wide margin, neither stamped — the shape the heuristic looks
        # for. Deliberately NOT the supersessions.jsonl pair (which already resolves via
        # an explicit record) so this exercises the inference path in isolation.
        DB.upsert_node(conn, _n("subj_old", "GnuCash migration status 2026-06-01",
                                "A/subj_old.md", "early note",
                                last_modified="2026-06-01T00:00:00"))
        DB.upsert_node(conn, _n("subj_new", "GnuCash migration status 2026-07-01",
                                "A/subj_new.md", "later note",
                                last_modified="2026-07-01T00:00:00"))
        conn.commit()

        candidates = VP.find_same_subject_candidates(conn)
        pair_ids = {(c["older_id"], c["newer_id"]) for c in candidates}
        assert ("subj_old", "subj_new") in pair_ids, candidates

        result = VP.run(conn=conn, apply=True, propose=True, verbose=False)
        assert result["proposals"]["candidates"] >= 1, result
        staged = result["proposals"]["staged_path"]
        assert staged and Path(staged).exists(), staged

        # The critical assertion: valid_until must NOT have been written from the
        # inference — only an explicit record may do that.
        row = conn.execute(
            "SELECT valid_until FROM nodes WHERE id='subj_old'").fetchone()
        assert row["valid_until"] is None, \
            "automatic inference wrote valid_until — it must only ever propose"

        staged_records = [json.loads(l) for l in Path(staged).read_text().splitlines() if l]
        assert any(r.get("older_id") == "subj_old" and r.get("newer_id") == "subj_new"
                  for r in staged_records), staged_records
        assert all(r.get("kind") == "temporal_validity_candidate" for r in staged_records)
        print("  ok  6. same-subject inference — stages a PROPOSAL, never writes "
              "valid_until itself")
    finally:
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


def run() -> int:
    test_stamp_from_supersessions_marks_old_not_new()
    test_stamp_from_contradictions_marks_invalidated()
    test_expired_tier_beats_any_score_advantage()
    test_expired_node_ranks_below_every_live_decoy()
    test_no_validity_data_ranks_identically_flag_on_off()
    test_migration_idempotent()
    test_same_subject_inference_only_proposes_never_applies()
    print("PASS — temporal validity: supersession/contradiction stamping, expired-tier "
          "ranking, no-data parity, migration idempotency, proposal-only inference")
    return 0


if __name__ == "__main__":
    sys.exit(run())
