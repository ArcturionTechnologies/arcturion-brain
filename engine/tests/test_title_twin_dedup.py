"""pytest coverage for content_dedup 7e — cross-hash SAME-TITLE twin collapse
(Leg 1, retrieval-precision programme, step 7.5).

7b (collapse_content_twins) only merges nodes sharing BOTH content_hash AND title. 7e
closes the gap: nodes sharing a title but not a content_hash because id-churn / reorg
path prefixes embedded different bytes into the file (frontmatter, etc.) while the
meaningful body stayed byte-identical. A synthetic group can contain several IDs and hashes but one title and body.

The gate is intentionally EXACT STRING EQUALITY of body_excerpt, not a fuzzy ratio —
near-identical but distinct bodies must remain separate. These tests cover both sides of that boundary: the true-positive (exact match) and the
false-positive shape (near-exact match) it must refuse.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from .. import config as C
from .. import db as DB


def _n(nid, title, path, ch, body="", summary=None, lm="2026-06-16T00:00:00Z",
       status="active", family="document", ntype="Source"):
    return {"id": nid, "type": ntype, "family": family, "title": title,
            "path": path, "owner": "X", "status": status, "governance_tier": 0,
            "content_hash": ch, "summary": summary if summary is not None else title,
            "body_excerpt": body, "centrality": 0.0,
            "created": None, "last_modified": lm, "last_reviewed": None, "metadata": {}}


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "brain.sqlite"
    monkeypatch.setattr(C, "BRAIN_DB", db_path)
    c = DB.connect(db_path)
    DB.init_db(c)
    yield c
    c.close()


@pytest.fixture()
def empty_fixtures_dir(tmp_path):
    """A fixtures dir with no golden/heldout files — _load_pinned_ids returns {}
    without touching the real live fixtures, so tests are hermetic by default."""
    d = tmp_path / "fixtures_empty"
    d.mkdir()
    return d


def _pinned_fixtures_dir(tmp_path, pinned_ids: list[str]) -> Path:
    d = tmp_path / "fixtures_pinned"
    d.mkdir()
    (d / "golden.jsonl").write_text(
        "\n".join(json.dumps({"id": f"g{i}", "class": "positive", "q": "q",
                              "expect": [pid]})
                  for i, pid in enumerate(pinned_ids)) + "\n",
        encoding="utf-8",
    )
    (d / "heldout.jsonl").write_text("", encoding="utf-8")
    return d


BODY = ("## Some Body\n\nThis is the exact body content shared by every reorg copy of "
        "this document, well past the minimum signature length threshold.")


def test_exact_body_match_collapses_cross_hash_same_title_twins(conn, empty_fixtures_dir):
    from ..maintenance import content_dedup as CD

    # Same title, three distinct content_hashes (id-churn from three root prefixes),
    # byte-identical body_excerpt — the op-biometric-banned shape.
    DB.upsert_node(conn, _n("mem_x_h1a", "op-biometric-banned", "root1/x.md", "h1", BODY,
                            lm="2026-05-03"))
    DB.upsert_node(conn, _n("mem_x_h1b", "op-biometric-banned", "root1b/x.md", "h1", BODY,
                            lm="2026-05-03"))  # same hash as h1a — not our concern (7b's)
    DB.upsert_node(conn, _n("mem_x_h2", "op-biometric-banned", "root2/x.md", "h2", BODY,
                            lm="2026-04-27"))
    DB.upsert_node(conn, _n("mem_x_h3", "op-biometric-banned", "root3/x.md", "h3", BODY,
                            lm="2026-05-02"))
    conn.commit()

    dry = CD.collapse_title_twins(conn, dry_run=True, verbose=False,
                                  fixtures_dir=empty_fixtures_dir)
    assert dry["groups"] == 1
    assert dry["merges"] == 3, dry  # 4 nodes, 1 canonical -> 3 losers

    live = CD.collapse_title_twins(conn, dry_run=False, verbose=False,
                                   fixtures_dir=empty_fixtures_dir)
    assert live["superseded"] == 3

    statuses = {r["id"]: r["status"] for r in
                conn.execute("SELECT id, status FROM nodes WHERE title='op-biometric-banned'")}
    active = [i for i, s in statuses.items() if s == "active"]
    assert len(active) == 1, statuses
    # newest last_modified among the group wins the final tiebreak (2026-05-03).
    assert active[0] in ("mem_x_h1a", "mem_x_h1b")

    # Idempotent: a second pass finds nothing left to merge.
    again = CD.collapse_title_twins(conn, dry_run=True, verbose=False,
                                    fixtures_dir=empty_fixtures_dir)
    assert again["merges"] == 0


def test_supersession_machinery_matches_7b(conn, empty_fixtures_dir):
    """Losers are SOFT-superseded (row survives, edges reparent, superseded_by edge
    emitted) — never hard-deleted. Same reversible contract as 7b/entity_dedup."""
    from ..maintenance import content_dedup as CD

    DB.upsert_node(conn, _n("mem_y_h1", "Some Doc", "a/doc.md", "h1", BODY))
    DB.upsert_node(conn, _n("mem_y_h2", "Some Doc", "b/doc.md", "h2", BODY,
                            lm="2026-07-01"))  # newer -> canonical
    DB.upsert_node(conn, {**_n("agent_x", "Agent X", "x/id.md", None),
                          "type": "Agent", "family": "concept"})
    DB.insert_edge(conn, {"from": "agent_x", "to": "mem_y_h1", "predicate": "authored",
                          "family": "structure", "asserted_by": "T"})
    conn.commit()

    live = CD.collapse_title_twins(conn, dry_run=False, verbose=False,
                                   fixtures_dir=empty_fixtures_dir)
    assert live["superseded"] == 1

    loser = conn.execute("SELECT status FROM nodes WHERE id='mem_y_h1'").fetchone()
    assert loser["status"] == "superseded"  # row survives, not deleted
    canonical = conn.execute("SELECT status FROM nodes WHERE id='mem_y_h2'").fetchone()
    assert canonical["status"] == "active"

    assert conn.execute(
        "SELECT 1 FROM edges WHERE from_id='mem_y_h1' AND predicate='superseded_by' "
        "AND to_id='mem_y_h2'").fetchone(), "superseded_by edge missing"
    assert conn.execute(
        "SELECT 1 FROM edges WHERE to_id='mem_y_h2' AND predicate='authored'").fetchone(), \
        "reparented edge missing on canonical"


def test_near_identical_but_not_exact_body_never_merges(conn, empty_fixtures_dir):
    """The false-positive shape this pass must refuse: same title, DIFFERENT nodes
    (e.g. two agents' own state files) whose bodies share a long boilerplate template
    but differ by one substituted token — high fuzzy similarity, NOT exact equality."""
    from ..maintenance import content_dedup as CD

    template = ("# {agent} Telegram Memory State\n\nThis file is maintained "
                "automatically by the local Arcturion bridge. It is the agent-side "
                "core for Telegram chat continuity across long sessions and restarts.")
    DB.upsert_node(conn, _n("mem_state_a", "state", "AGENTA/telegram/state.md", "ha",
                            template.format(agent="AGENTA")))
    DB.upsert_node(conn, _n("mem_state_b", "state", "AGENTB/telegram/state.md", "hb",
                            template.format(agent="AGENTB")))
    conn.commit()

    dry = CD.collapse_title_twins(conn, dry_run=True, verbose=False,
                                  fixtures_dir=empty_fixtures_dir)
    assert dry["merges"] == 0, "near-duplicate templated bodies must never merge"

    both_active = [r["status"] for r in
                   conn.execute("SELECT status FROM nodes WHERE title='state'")]
    assert both_active == ["active", "active"]


def test_single_hash_per_title_is_left_to_7b(conn, empty_fixtures_dir):
    """A title bucket where every member shares ONE content_hash is 7b's job (exact
    content_hash+title match) — 7e must not touch it (guards against double-processing
    or 7e silently subsuming 7b's simpler, cheaper path)."""
    from ..maintenance import content_dedup as CD

    DB.upsert_node(conn, _n("mem_z_1", "Same Everything", "a/z.md", "h1", BODY))
    DB.upsert_node(conn, _n("mem_z_2", "Same Everything", "b/z.md", "h1", BODY))
    conn.commit()

    dry = CD.collapse_title_twins(conn, dry_run=True, verbose=False,
                                  fixtures_dir=empty_fixtures_dir)
    assert dry["groups"] == 0 and dry["merges"] == 0


def test_oversized_title_bucket_is_skipped_for_cost_safety(conn, empty_fixtures_dir):
    """A title with more distinct content_hashes than MAX_DISTINCT_HASHES_PER_TITLE is
    skipped entirely rather than paying for a pairwise scan that never finds
    an exact match anyway."""
    from ..maintenance import content_dedup as CD

    n = CD.MAX_DISTINCT_HASHES_PER_TITLE + 1
    for i in range(n):
        DB.upsert_node(conn, _n(f"mem_big_{i}", "Generic Title", f"p{i}/x.md", f"h{i}", BODY))
    conn.commit()

    dry = CD.collapse_title_twins(conn, dry_run=True, verbose=False,
                                  fixtures_dir=empty_fixtures_dir)
    assert dry["merges"] == 0
    assert dry["skipped_oversized_titles"] == 1


def test_fixture_pinned_id_wins_and_loser_gets_alias(conn, tmp_path):
    """The golden-fixture contract (Leg 1 hard constraint): when a demotion loser is a
    fixture-pinned id, the winner adopts it as an alias, AND — because supersession is
    soft — the loser row still resolves for check_expected_ids regardless."""
    from ..maintenance import content_dedup as CD

    DB.upsert_node(conn, _n("mem_p_h1", "Pinned Doc", "a/p.md", "h1", BODY, lm="2026-01-01"))
    DB.upsert_node(conn, _n("mem_p_h2", "Pinned Doc", "b/p.md", "h2", BODY, lm="2026-06-01"))
    conn.commit()

    fixtures_dir = _pinned_fixtures_dir(tmp_path, ["mem_p_h1"])

    # Without the pin, newest last_modified (mem_p_h2) would win.
    dry_unpinned = CD.collapse_title_twins(conn, dry_run=True, verbose=False,
                                           fixtures_dir=tmp_path / "fixtures_empty_missing")
    assert dry_unpinned["sample"][0]["canonical"] == "mem_p_h2"

    live = CD.collapse_title_twins(conn, dry_run=False, verbose=False,
                                   fixtures_dir=fixtures_dir)
    assert live["superseded"] == 1
    # The loser (mem_p_h2) is NOT itself pinned, so no alias is needed here — the
    # win-preference alone is enough to keep the pinned id resolvable. Alias adoption
    # for a pinned LOSER is covered separately below
    # (test_two_pinned_ids_in_one_group_loser_still_gets_alias).
    assert live["aliases_adopted"] == []

    # The pinned id is now the ACTIVE canonical (never demoted when it can be avoided).
    pinned_status = conn.execute("SELECT status FROM nodes WHERE id='mem_p_h1'").fetchone()
    assert pinned_status["status"] == "active"
    loser_status = conn.execute("SELECT status FROM nodes WHERE id='mem_p_h2'").fetchone()
    assert loser_status["status"] == "superseded"


def test_two_pinned_ids_in_one_group_loser_still_gets_alias(conn, tmp_path):
    """Rare but load-bearing: if BOTH members of a twin cluster are separately pinned
    (e.g. a golden `expect` and its `alternates` entry, as with the real
    mem_op_biometric_banned_a942e0 / _46e21f pair), one of them must still lose — and
    it must get the alias so its fixture entry keeps resolving to the surviving id."""
    from ..maintenance import content_dedup as CD

    DB.upsert_node(conn, _n("mem_q_h1", "Both Pinned", "a/q.md", "h1", BODY))
    DB.upsert_node(conn, _n("mem_q_h2", "Both Pinned", "b/q.md", "h2", BODY))
    conn.commit()

    fixtures_dir = _pinned_fixtures_dir(tmp_path, ["mem_q_h1", "mem_q_h2"])
    live = CD.collapse_title_twins(conn, dry_run=False, verbose=False,
                                   fixtures_dir=fixtures_dir)
    assert live["superseded"] == 1
    # deterministic: lowest id wins among the pinned candidates.
    assert live["aliases_adopted"] == [("mem_q_h2", "mem_q_h1")]


def test_7b_also_respects_pinned_ids(conn, tmp_path):
    """7b (collapse_content_twins) runs before 7e in the documented order and must not
    silently demote a pinned id out from under 7e before 7e ever sees it."""
    from ..maintenance import content_dedup as CD

    DB.upsert_node(conn, _n("mem_r_live", "Doctrine", "live/doc.md", "hh", lm="2026-06-10"))
    DB.upsert_node(conn, _n("mem_r_arch", "Doctrine", "arch/doc.md", "hh", lm="2026-06-12"))
    conn.commit()

    fixtures_dir = _pinned_fixtures_dir(tmp_path, ["mem_r_arch"])
    live = CD.collapse_content_twins(conn, dry_run=False, verbose=False,
                                     fixtures_dir=fixtures_dir)
    assert live["superseded"] == 1
    assert live["aliases_adopted"] == []  # not needed — the pinned id WON
    status = conn.execute("SELECT status FROM nodes WHERE id='mem_r_arch'").fetchone()
    assert status["status"] == "active"


def test_duplicate_rate_metric_reflects_both_detectors(conn, empty_fixtures_dir, monkeypatch):
    """`duplicate_rate` is content_dedup's own metric: merges detectable by 7b + 7e,
    over active nodes with a content_hash. It must drop to 0 once both passes have
    actually collapsed every twin group they found."""
    from ..maintenance import content_dedup as CD

    # A pure 7e case (cross-hash, same title, exact body).
    DB.upsert_node(conn, _n("mem_s_h1", "Twin", "a/s.md", "h1", BODY))
    DB.upsert_node(conn, _n("mem_s_h2", "Twin", "b/s.md", "h2", BODY))
    # A distinct, unrelated singleton — must not be counted as a duplicate.
    DB.upsert_node(conn, _n("mem_solo", "Solo Doc", "c/solo.md", "h3", "unrelated body"))
    conn.commit()

    # duplicate_rate() always loads pins from the real on-disk fixtures path; point it
    # at the empty one for this hermetic test via monkeypatch so it can't pick up the
    # live golden/heldout fixtures.
    monkeypatch.setattr(CD, "_FIXTURES_DIR", empty_fixtures_dir)

    before = CD.duplicate_rate(conn)
    assert before["duplicate_nodes"] == 1
    assert before["total_active_with_hash"] == 3
    assert before["duplicate_rate"] == pytest.approx(1 / 3)

    CD.collapse_content_twins(conn, dry_run=False, verbose=False)
    CD.collapse_title_twins(conn, dry_run=False, verbose=False)

    after = CD.duplicate_rate(conn)
    assert after["duplicate_nodes"] == 0
    assert after["duplicate_rate"] == 0.0
