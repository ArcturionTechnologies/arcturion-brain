"""Hermetic unit tests for Leg 3 seeding fixes (retrieval-honesty programme, 2026-08-06):
contextual embedding prefixes and query-side alias expansion. No pytest.

Covers:
  1. `embedder._context_header` — composition (title/shelf/owner/kind) and graceful
     degrade when a field is missing.
  2. `embedder._text_for_embedding` — `C.CONTEXTUAL_EMBED_PREFIX` off restores the
     pre-Leg-3 embedding input BYTE-IDENTICAL; on, the header is prepended.
  3. `aliases.expand_query_terms` — a registered alias phrase pulls in the rest of its
     canonical's alias words; no hit is a no-op; the cap is respected.
  4. `brain._seed_channels` wiring — `C.QUERY_ALIAS_EXPANSION` off leaves the FTS seed
     term list untouched; on, an alias hit reaches the FTS OR-seed (survives the
     FTS_SEED_IDF_FILTER, which would otherwise drop any term missing from `stats`).

Run:  python3 -m engine.tests.test_contextual_prefix
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

from .. import config as C
from .. import db as DB


def _n(nid, title, path, summary="", owner="ALTAIR", prong="hq", node_type="Source",
       body=None):
    return {"id": nid, "type": node_type, "family": "document", "title": title,
            "path": path, "owner": owner, "status": "active", "governance_tier": 0,
            "content_hash": None, "summary": summary, "body_excerpt": body,
            "centrality": 0.0, "created": "2026-08-01T00:00:00Z",
            "last_modified": "2026-08-01T00:00:00Z", "last_reviewed": None,
            "metadata": {}, "prong": prong}


# --------------------------------------------------------------- 1 & 2. embedder


def test_context_header_composition() -> None:
    from .. import embedder as E

    row = {"title": "Thermal and rate gates", "prong": "hq", "owner": "ALTAIR",
           "type": "Preference"}
    header = E._context_header(row)
    assert header == "Thermal and rate gates | shelf: hq | owner: ALTAIR | kind: Preference", (
        f"unexpected header composition: {header!r}")
    print("  ok  1. context header composes title | shelf | owner | kind in order")


def test_context_header_degrades_gracefully_on_missing_fields() -> None:
    from .. import embedder as E

    # A plain dict with only 'title' present — simulates a row a caller selected without
    # the extra Leg-3 columns. Must not raise, must not emit 'None' or a dangling ' | '.
    row = {"title": "Bare Node"}
    header = E._context_header(row)
    assert header == "Bare Node", f"missing fields leaked into the header: {header!r}"

    # No title either — every field absent.
    header_empty = E._context_header({})
    assert header_empty == "", f"an all-empty row must compose an empty header: {header_empty!r}"
    print("  ok  2. context header degrades gracefully when fields are absent (no crash, "
          "no 'None', no dangling separator)")


def test_text_for_embedding_flag_off_byte_identical_to_pre_leg3() -> None:
    from .. import embedder as E

    row = {"title": "Thermal and rate gates", "summary": "how the thermal gate works",
           "body_excerpt": "detailed body text here", "prong": "hq", "owner": "ALTAIR",
           "type": "Preference"}
    orig = C.CONTEXTUAL_EMBED_PREFIX
    try:
        C.CONTEXTUAL_EMBED_PREFIX = False
        got = E._text_for_embedding(row)
        # The pre-Leg-3 computation, reproduced literally: title/summary/body_excerpt
        # joined on '\n', empties dropped.
        want = "\n".join(p for p in (row["title"], row["summary"], row["body_excerpt"]) if p)
        assert got == want, f"flag-off embedding input diverged from pre-Leg-3: {got!r} != {want!r}"
    finally:
        C.CONTEXTUAL_EMBED_PREFIX = orig
    print("  ok  3. CONTEXTUAL_EMBED_PREFIX=off — embedding input byte-identical to the "
          "pre-Leg-3 computation")


def test_text_for_embedding_flag_on_prepends_header() -> None:
    from .. import embedder as E

    row = {"title": "Thermal and rate gates", "summary": "how the thermal gate works",
           "body_excerpt": "detailed body text here", "prong": "hq", "owner": "ALTAIR",
           "type": "Preference"}
    orig = C.CONTEXTUAL_EMBED_PREFIX
    try:
        C.CONTEXTUAL_EMBED_PREFIX = True
        got = E._text_for_embedding(row)
        header = E._context_header(row)
        body = "\n".join(p for p in (row["title"], row["summary"], row["body_excerpt"]) if p)
        assert got == f"{header}\n{body}", f"header not prepended correctly: {got!r}"
        assert got.startswith("Thermal and rate gates | shelf: hq"), got
    finally:
        C.CONTEXTUAL_EMBED_PREFIX = orig
    print("  ok  4. CONTEXTUAL_EMBED_PREFIX=on — the context header is prepended to the "
          "embedding input, stored body untouched")


def test_text_for_embedding_never_touches_stored_body() -> None:
    """The prefix is an EMBEDDING-INPUT-only change. `_text_for_embedding`'s return value
    feeds the encoder, not `nodes.body_excerpt` / the FTS index — this test pins that the
    function reads `body_excerpt` but never mutates the input row, so a caller can rely on
    the row still being the untouched DB record afterwards."""
    from .. import embedder as E

    row = {"title": "T", "summary": "S", "body_excerpt": "B", "prong": "hq",
           "owner": "ALTAIR", "type": "Source"}
    snapshot = dict(row)
    orig = C.CONTEXTUAL_EMBED_PREFIX
    try:
        C.CONTEXTUAL_EMBED_PREFIX = True
        E._text_for_embedding(row)
    finally:
        C.CONTEXTUAL_EMBED_PREFIX = orig
    assert row == snapshot, "embedding-input composition mutated the source row"
    print("  ok  5. embedding-input composition never mutates the source row / stored body")


# --------------------------------------------------------------- 3. alias expansion


def test_expand_query_terms_on_alias_hit() -> None:
    from .. import aliases as A

    canonical_to_aliases = {
        "mem_thermal_and_rate_gates_d6117f": [
            "thermal gate", "heat gate", "thermal throttling",
            "stop the laptop from overheating",
        ],
    }
    topic = "how do we stop the laptop from overheating when it runs models"
    terms = ["stop", "laptop", "overheating", "runs", "models"]
    expanded = A.expand_query_terms(topic, terms, canonical_to_aliases, cap=8)
    added = [t for t in expanded if t not in terms]
    assert "thermal" in added and "gate" in added, (
        f"alias hit did not pull corpus jargon into the term list: {added}")
    # Original terms preserved, in order, at the front.
    assert expanded[: len(terms)] == terms
    print("  ok  6. expand_query_terms — a registered alias phrase adds the canonical's "
          "other alias words to the seed term list")


def test_expand_query_terms_no_hit_is_noop() -> None:
    from .. import aliases as A

    canonical_to_aliases = {
        "mem_thermal_and_rate_gates_d6117f": ["thermal gate", "heat gate"],
    }
    topic = "what is the weather like today"
    terms = ["weather", "today"]
    expanded = A.expand_query_terms(topic, terms, canonical_to_aliases, cap=8)
    assert expanded == terms, f"no alias phrase present but terms changed: {expanded}"
    print("  ok  7. expand_query_terms — no registered alias phrase in the query is a no-op")


def test_expand_query_terms_respects_cap() -> None:
    from .. import aliases as A

    canonical_to_aliases = {
        "mem_x": ["trigger phrase",
                  "alpha beta gamma delta epsilon zeta eta theta iota kappa"],
    }
    topic = "this contains the trigger phrase somewhere"
    terms = ["this", "contains", "the", "somewhere"]
    expanded = A.expand_query_terms(topic, terms, canonical_to_aliases, cap=3)
    added = [t for t in expanded if t not in terms]
    assert len(added) <= 3, f"cap of 3 was not respected: {added}"
    print("  ok  8. expand_query_terms — the new-term cap is respected")


def test_expand_query_terms_wildcard_alias_never_fires() -> None:
    """A single- or double-character alias must never match — guards against a future
    ENTITY_ALIASES.md row with a too-short alias turning into an effective wildcard that
    matches almost every query."""
    from .. import aliases as A

    canonical_to_aliases = {"mem_x": ["ab", "a real alias phrase here"]}
    topic = "ab initio calculations for the ab project"
    terms = ["ab", "initio", "calculations", "for", "the", "project"]
    expanded = A.expand_query_terms(topic, terms, canonical_to_aliases, cap=8)
    assert expanded == terms, (
        f"a 2-char alias fired as if it were a real phrase match: {expanded}")
    print("  ok  9. expand_query_terms — a too-short alias never fires as a match")


# --------------------------------------------------------------- 4. brain._seed_channels wiring


def _alias_seed_fixture_db(tmp: Path, canon_path: Path):
    conn = DB.connect()
    DB.init_db(conn)
    DB.upsert_node(conn, _n(
        "mem_thermal_and_rate_gates_d6117f", "Thermal and rate gates",
        "Memory/Preferences/thermal.md",
        summary="the thermal gate throttles local model inference"))
    conn.commit()
    return conn


def test_seed_channels_alias_expansion_reaches_fts_seed() -> None:
    """End-to-end: a Founder-phrased query that never says 'thermal gate' still reaches
    the FTS OR-seed's term list once QUERY_ALIAS_EXPANSION is on, and the expanded terms
    survive FTS_SEED_IDF_FILTER (which would otherwise drop any term missing from
    `stats`) because `_seed_channels` merges fresh IDF stats for the added terms."""
    from .. import brain, rank_signals as RS, aliases as A

    tmp = Path(tempfile.mkdtemp(prefix="brain_ctxprefix_seed_test_"))
    canon_dir = tmp / "canon"
    canon_dir.mkdir()
    alias_md = canon_dir / "ENTITY_ALIASES.md"
    alias_md.write_text(
        "## Concepts\n\n"
        "| Canonical | Aliases |\n"
        "|---|---|\n"
        "| `mem_thermal_and_rate_gates_d6117f` | thermal gate, "
        "stop the laptop from overheating |\n",
        encoding="utf-8",
    )
    orig_db = C.BRAIN_DB
    orig_alias_path = C.ENTITY_ALIASES
    orig_qae = C.QUERY_ALIAS_EXPANSION
    orig_fts_filter = C.FTS_SEED_IDF_FILTER
    C.BRAIN_DB = tmp / "brain.sqlite"
    C.ENTITY_ALIASES = alias_md
    try:
        RS.reset_cache()
        A.reset_cache()
        conn = _alias_seed_fixture_db(tmp, alias_md)
        stats = {}  # empty, like a v1-shaped caller — exercises the falsy-stats branch too

        topic = "how do we stop the laptop from overheating"
        C.QUERY_ALIAS_EXPANSION = False
        channels_off, _ = brain._seed_channels(conn, topic, 12, stats=stats)

        C.QUERY_ALIAS_EXPANSION = True
        C.FTS_SEED_IDF_FILTER = True
        channels_on, _ = brain._seed_channels(conn, topic, 12, stats=stats)

        assert "mem_thermal_and_rate_gates_d6117f" not in channels_off.get("fts", []), (
            "flag off already found the node via literal FTS terms — fixture cannot "
            "distinguish the flag's effect; strengthen the negative control")
        assert "mem_thermal_and_rate_gates_d6117f" in channels_on.get("fts", []), (
            f"alias expansion did not reach the FTS seed: {channels_on.get('fts', [])}")
        conn.close()
        print("  ok 10. brain._seed_channels — QUERY_ALIAS_EXPANSION reaches the FTS "
              "OR-seed and survives FTS_SEED_IDF_FILTER")
    finally:
        C.BRAIN_DB = orig_db
        C.ENTITY_ALIASES = orig_alias_path
        C.QUERY_ALIAS_EXPANSION = orig_qae
        C.FTS_SEED_IDF_FILTER = orig_fts_filter
        RS.reset_cache()
        A.reset_cache()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_context_header_composition()
    test_context_header_degrades_gracefully_on_missing_fields()
    test_text_for_embedding_flag_off_byte_identical_to_pre_leg3()
    test_text_for_embedding_flag_on_prepends_header()
    test_text_for_embedding_never_touches_stored_body()
    test_expand_query_terms_on_alias_hit()
    test_expand_query_terms_no_hit_is_noop()
    test_expand_query_terms_respects_cap()
    test_expand_query_terms_wildcard_alias_never_fires()
    test_seed_channels_alias_expansion_reaches_fts_seed()
    print("PASS — Leg 3 seeding fixes: contextual embed prefix (composition, degrade, "
          "flag-off byte-identity, no stored-body mutation) + query alias expansion "
          "(hit/no-hit/cap/wildcard-guard) + brain._seed_channels wiring")
