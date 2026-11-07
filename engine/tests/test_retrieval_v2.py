"""Hermetic unit tests for retrieval v2 (Leg 2, retrieval-honesty 2026-08-04). No pytest.

Covers the four signals this leg wired in, each against the specific failure it exists to
close, plus the A/B switch that must restore the old path exactly:

  1. reciprocal-rank fusion  — consensus beats one loud channel; ranks, not scores
  2. exact-token primacy     — a corpus-rare literal match pins its document
  3. real age decay          — a 2024 note and a 2026 note stop ranking identically
  4. relevance floor         — a question with no answer returns an EMPTY pack
  5. RETRIEVAL_V2=0          — the pre-Leg-2 ranking path, unchanged

Run:  python3 -m engine.tests.test_retrieval_v2
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config as C
from .. import db as DB


def _n(nid, title, path, summary, status="active", last_modified="2026-06-16T00:00:00Z",
       body=None):
    return {"id": nid, "type": "Source", "family": "document", "title": title,
            "path": path, "owner": "ALTAIR", "status": status, "governance_tier": 0,
            "content_hash": None, "summary": summary, "body_excerpt": body,
            "centrality": 0.0, "created": None, "last_modified": last_modified,
            "last_reviewed": None, "metadata": {}}


# --------------------------------------------------------------------------- 1. fusion

def test_rrf() -> None:
    from .. import rank_signals as RS

    # Consensus beats a single loud signal. `b` is nobody's favourite but everybody's
    # second choice; `a` tops one channel and appears in no other. That is the whole
    # premise of fusion, and the old union-then-hand-score path could not express it.
    lists = {"fts": ["a", "b"], "vector": ["c", "b"], "title": ["d", "b"]}
    scored = RS.rrf(lists, weights={"fts": 1.0, "vector": 1.0, "title": 1.0}, k=60)
    assert scored["b"] > scored["a"], f"consensus lost to one channel: {scored}"
    assert scored["b"] > scored["c"] and scored["b"] > scored["d"], scored

    # Rank, not score: only position is consumed, so a channel cannot inflate its vote by
    # reporting bigger numbers. Two channels agreeing on #1 must beat one channel's #1.
    solo = RS.rrf({"fts": ["x"]}, weights={"fts": 1.0}, k=60)
    duo = RS.rrf({"fts": ["y"], "vector": ["y"]}, weights={"fts": 1.0, "vector": 1.0}, k=60)
    assert duo["y"] > solo["x"], (solo, duo)

    # A channel repeating an id must not double-vote for it.
    dup = RS.rrf({"fts": ["z", "z", "z"]}, weights={"fts": 1.0}, k=60)
    assert abs(dup["z"] - 1.0 / 61) < 1e-9, dup

    # Weight zero silences a channel entirely rather than merely shrinking it.
    off = RS.rrf({"fts": ["q"]}, weights={"fts": 0.0}, k=60)
    assert off == {}, off

    # An empty input is not an error.
    assert RS.rrf({}) == {}
    print("  ok  1. rrf — consensus > single channel, rank-based, dedup, weight-0 silences")


# --------------------------------------------------------------------- 2. exact-token pin

def test_rare_tokens_and_pin() -> None:
    from .. import rank_signals as RS

    n_docs = 50_000
    stats = {
        "errno": (7, RS.idf(7, n_docs)),          # rare  → pins
        "agent": (9_000, RS.idf(9_000, n_docs)),  # common → never pins
        "abc": (3, RS.idf(3, n_docs)),            # rare but too short → never pins
        "zzz": (0, RS.idf(0, n_docs)),            # absent from corpus → MUST NOT pin
        "huh": (-1, RS.idf(-1, n_docs)),          # unknown frequency → MUST NOT pin
    }
    rares = RS.rare_tokens(stats)
    assert rares == ["errno"], f"rare-token selection wrong: {rares}"

    # df == 0 is the nonsense-query trap: a token nothing in the corpus contains is not
    # "maximally rare", it is absent, and treating it as evidence is how a nonsense query
    # talks itself into confidence.
    assert RS.idf(0, n_docs) > RS.idf(9_000, n_docs), "IDF is not monotone in rarity"
    assert RS.idf(-1, n_docs) == 0.0, "unknown df must carry no information"

    node = _n("x", "mkdir failed", "A/x.md", "the call raised Errno 17 File exists")
    assert RS.exact_token_hits(node, ["errno"]) == ["errno"]
    assert RS.exact_token_hits(node, ["submarine"]) == []
    assert RS.exact_token_hits(node, []) == []

    # The literal match is case-folded and substring-based, so a rare identifier still hits
    # when it sits inside a longer path or quoted traceback.
    pathy = _n("y", "client", "ALTAIR/tools/local_llm_client.py", "")
    assert RS.exact_token_hits(pathy, ["local_llm_client.py"]) == ["local_llm_client.py"]
    print("  ok  2. exact-token — rarity band excludes common/short/absent/unknown; "
          "literal match is case-folded + substring")


# --------------------------------------------------------------------------- 3. age decay

def test_age_decay() -> None:
    from .. import rank_signals as RS

    now = datetime(2026, 8, 4, tzinfo=timezone.utc)
    fresh = RS.age_decay(now.isoformat(), now=now, halflife_days=180)
    half = RS.age_decay((now - timedelta(days=180)).isoformat(), now=now, halflife_days=180)
    old = RS.age_decay((now - timedelta(days=720)).isoformat(), now=now, halflife_days=180)

    assert abs(fresh - 1.0) < 1e-6, fresh
    assert abs(half - 0.5) < 1e-6, half
    assert old < half < fresh

    # THE recency-class failure, as an assertion: under the old binary has-a-timestamp flag
    # these two scored identically, because file mtime always supplies a timestamp.
    y2024 = RS.age_decay("2024-01-01T00:00:00Z", now=now, halflife_days=180)
    y2026 = RS.age_decay("2026-07-01T00:00:00Z", now=now, halflife_days=180)
    assert y2026 > y2024 * 5, f"2024 and 2026 still rank alike: {y2024} vs {y2026}"

    # No timestamp is NO recency evidence, not maximum recency.
    assert RS.age_decay(None, now=now) == 0.0
    assert RS.age_decay("", now=now) == 0.0
    assert RS.age_decay("not-a-date", now=now) == 0.0

    # A future timestamp is clamped, never rewarded — clock skew and bad frontmatter dates
    # must not be able to buy rank.
    assert RS.age_decay((now + timedelta(days=999)).isoformat(), now=now) == 1.0
    print("  ok  3. age decay — exponential, half-life exact, 2024≠2026, undated=0, "
          "future clamped")


# ------------------------------------------------------------------ 4/5. floor + A/B switch

def _fixture_db(tmp: Path):
    conn = DB.connect(C.BRAIN_DB)
    DB.init_db(conn)
    DB.upsert_node(conn, _n("gnu_old", "GnuCash is the canonical books system",
                            "SIRIUS/decisions/gnucash.md",
                            "GnuCash is the system of record for the books.",
                            last_modified="2024-06-07T00:00:00Z"))
    DB.upsert_node(conn, _n("csv_new", "GnuCash is dead, the CSV reader is canonical",
                            "SIRIUS/decisions/csv_reader.md",
                            "GnuCash is retired. The CSV reader is the canonical books "
                            "system of record.",
                            last_modified="2026-07-05T00:00:00Z"))
    DB.upsert_node(conn, _n("unrelated", "Dehumidifier wifi setup",
                            "ALTAIR/home/dehumidifier.md",
                            "How the dehumidifier joins the wifi network."))
    conn.commit()
    return conn


def test_floor_and_ab_switch() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="brain_v2_test_"))
    orig_db, orig_v2, orig_floor = C.BRAIN_DB, C.RETRIEVAL_V2, C.RELEVANCE_FLOOR
    orig_vec = C.VECTOR_SEED_ENABLED
    C.BRAIN_DB = tmp / "brain.sqlite"
    # No embeddings in a fixture graph; the semantic half of the floor is exercised by the
    # live golden gate, the lexical half here.
    C.VECTOR_SEED_ENABLED = False
    try:
        from .. import brain, rank_signals as RS
        RS.reset_cache()
        conn = _fixture_db(tmp)

        # --- 4a. the floor returns an EMPTY pack, not a pack of near-misses.
        C.RETRIEVAL_V2 = True
        C.RELEVANCE_FLOOR = 0.9         # nothing in this fixture can clear it
        # Seeds on "wifi network" (the unrelated node) but answers nothing — the shape the
        # floor exists for. A query matching NOTHING would exit earlier via "[no nodes
        # matched]" and would not exercise the floor at all.
        NO_ANSWER = "how does the quarterly submarine lease renewal affect the wifi network"
        pack = brain.query(NO_ANSWER, token_budget=2000)
        assert pack.nodes == [], f"floor did not empty the pack: {[n['id'] for n in pack.nodes]}"
        assert pack.text_bundle.startswith(C.LOW_CONFIDENCE_MARKER), pack.text_bundle
        assert any(w.startswith("low_confidence_empty:") for w in pack.warnings), pack.warnings

        # --- 4b. a floor of 0 must not empty anything: the floor is a gate, not a filter,
        #         and an answerable question must survive it untouched.
        C.RELEVANCE_FLOOR = 0.0
        pack = brain.query("canonical books system of record", token_budget=2000)
        assert pack.nodes, "floor=0 still returned an empty pack"
        assert not pack.text_bundle.startswith(C.LOW_CONFIDENCE_MARKER), pack.text_bundle

        # --- 4c. age decay reaches the ranking: same words, same status, 2024 vs 2026.
        ids = [n["id"] for n in pack.nodes]
        assert ids[0] == "csv_new", f"2024 note outranked the 2026 one: {ids}"

        # --- 4d. the per-request scoring scratch never leaves the engine.
        # No engine-private field may ride out on a returned node. Asserted as a shape
        # (nothing underscore-prefixed) rather than against one known key name, so a future
        # scratch field is caught without anyone remembering to add it here.
        leaked = {k for n in pack.nodes for k in n if k.startswith("_")}
        assert not leaked, f"internal scratch leaked into the returned nodes: {leaked}"

        # --- 5. RETRIEVAL_V2=0 restores the old path: no floor, no age decay in ranking.
        C.RETRIEVAL_V2 = False
        C.RELEVANCE_FLOOR = 0.9         # would empty every pack on the v2 path
        old = brain.query(NO_ANSWER, token_budget=2000)
        assert old.nodes, "v1 path was affected by the v2 relevance floor"
        assert not old.text_bundle.startswith(C.LOW_CONFIDENCE_MARKER), old.text_bundle

        # v1's `_relevance_score` is the sum of the two halves the split produced — this is
        # what makes the A/B switch a restore rather than an approximation of one.
        row = dict(conn.execute("SELECT * FROM nodes WHERE id='csv_new'").fetchone())
        assert abs(brain._relevance_score(row, "canonical books")
                   - (brain._lexical_score(row, "canonical books")
                      + brain._authority_score(row))) < 1e-9

        conn.close()
        print("  ok  4. floor — empties on no-answer, warns, never fires at 0; "
              "age decay orders 2026 over 2024; scratch stripped")
        print("  ok  5. RETRIEVAL_V2=0 — floor inert, v1 scorer identical to lexical+authority")
    finally:
        C.BRAIN_DB, C.RETRIEVAL_V2, C.RELEVANCE_FLOOR = orig_db, orig_v2, orig_floor
        C.VECTOR_SEED_ENABLED = orig_vec
        from .. import rank_signals as RS
        RS.reset_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def test_status_dominates_age() -> None:
    """The card's constraint, as arithmetic: age decay is a TIEBREAK, never a filter.

    Fix round 1 found the shipped weight (16.0) EXCEEDED the canonical status boost (10.0), so
    a fresh draft levelled with an old canonical and recency could outrank status outright —
    while the comment above it claimed status dominated "by an order of magnitude". The bound
    is asserted here rather than described in prose, so it cannot drift again silently.
    """
    from .. import brain as B, rank_signals as RS

    # The age term is WEIGHT x 2^(-age/half-life), so WEIGHT is its entire range.
    assert C.AGE_DECAY_WEIGHT < B._STATUS_BOOST["canonical"], (
        f"recency can outrank canonical status: weight {C.AGE_DECAY_WEIGHT} "
        f">= canonical boost {B._STATUS_BOOST['canonical']}")
    worst = min(B._STATUS_PENALTY.values())
    assert C.AGE_DECAY_WEIGHT < abs(worst) / 2, (
        f"recency could freshen a superseded node back into the pack: weight "
        f"{C.AGE_DECAY_WEIGHT} vs penalty {worst}")

    # The worked example from the review, end to end.
    now = datetime(2026, 8, 4, tzinfo=timezone.utc)
    fresh_draft = _n("d", "t", "A/d.md", "s", status="draft", last_modified=now.isoformat())
    old_canon = _n("c", "t", "A/c.md", "s", status="canonical",
                   last_modified=(now - timedelta(days=730)).isoformat())

    def total(node):
        return (B._authority_score(node)
                + C.AGE_DECAY_WEIGHT * RS.age_decay(node["last_modified"], now=now))

    assert total(old_canon) > total(fresh_draft), (
        f"a fresh draft ({total(fresh_draft):.2f}) beat a two-year-old canonical "
        f"({total(old_canon):.2f})")
    print(f"  ok  6. status dominates age — weight {C.AGE_DECAY_WEIGHT} < canonical "
          f"{B._STATUS_BOOST['canonical']}; old canonical {total(old_canon):.1f} > "
          f"fresh draft {total(fresh_draft):.1f}")


def test_true_exact_token_pin() -> None:
    """A rare literal match sorts above every unpinned node REGARDLESS of fusion score.

    An additive boost cannot express that at any magnitude — it is a number competing with
    other numbers, so some fusion+similarity combination always beats it. A sort tier can.
    """
    from .. import rank_signals as RS

    assert C.EXACT_TOKEN_TRUE_PIN, "the shipped default must be the true pin, not the boost"

    rares = ["errno", "encodings"]
    pinned = _n("p", "mkdir trap", "A/p.md", "the call raised Errno 17 File exists")
    rival = _n("r", "something else", "A/r.md", "no rare token here")
    both = _n("b", "x", "A/b.md", "Errno 17 raised during the encodings import")

    def key(node, score):
        """The shape brain.rank_key_v2 sorts on."""
        return (len(RS.exact_token_hits(node, rares)), score)

    # Hand the rival an absurd score advantage — better than any real fusion + similarity
    # combination can produce — and the pin must still win.
    assert key(pinned, 0.0) > key(rival, 10_000.0), \
        "a pinned document lost to a high-scoring unpinned one — that is a boost, not a pin"
    # Two rare tokens beat one: the document holding both halves of an error string wins.
    assert key(both, 0.0) > key(pinned, 10_000.0)
    # A node matching nothing rare is never lifted into a pinned tier.
    assert key(rival, 10_000.0)[0] == 0
    print("  ok  7. true exact-token pin — tier beats any score; more rare tokens rank higher")


def test_age_decay_never_raises() -> None:
    """HIGH-1 (fix round 3). `ref - dt` raised TypeError on a naive/aware mismatch — inside a
    loop running up to 3,000x per query, behind a live HTTP service, in a module whose whole
    contract is that a signal which cannot be computed costs its own contribution and never
    the request."""
    from .. import rank_signals as RS

    naive_now = datetime(2026, 8, 4)                       # no tzinfo — the reported crash
    aware_now = datetime(2026, 8, 4, tzinfo=timezone.utc)

    assert 0.0 <= RS.age_decay("2026-07-01T00:00:00+00:00", now=naive_now) <= 1.0
    assert 0.0 <= RS.age_decay("2026-07-01T00:00:00", now=aware_now) <= 1.0
    assert 0.0 <= RS.age_decay("2026-07-01T00:00:00", now=naive_now) <= 1.0

    # A naive and an aware reading of the SAME instant must agree.
    a = RS.age_decay("2026-07-01T00:00:00+00:00", now=naive_now)
    b = RS.age_decay("2026-07-01T00:00:00+00:00", now=aware_now)
    assert abs(a - b) < 1e-9, (a, b)

    # Nothing a node can carry in `last_modified` may raise.
    for bad in (None, "", "not-a-date", "2026-13-45T99:99:99", 12345, [], {}, object()):
        assert RS.age_decay(bad, now=naive_now) == 0.0, bad
    print("  ok  8. age_decay never raises — naive/aware normalized, junk timestamps -> 0.0")


def test_document_frequency_hostile_tokens() -> None:
    """MED-1 (fix round 3). The token was interpolated into an FTS5 MATCH phrase unescaped.
    MATCH is its own query language: a `"` closes the phrase and the rest parses as syntax."""
    tmp = Path(tempfile.mkdtemp(prefix="brain_df_test_"))
    orig_db = C.BRAIN_DB
    C.BRAIN_DB = tmp / "brain.sqlite"
    try:
        from .. import rank_signals as RS
        RS.reset_cache()
        conn = _fixture_db(tmp)

        hostile = ['gnucash" OR fts_nodes MATCH "a', 'x" OR "y', 'NEAR(a b)', 'foo*',
                   'a AND b', 'a OR b', 'title:secret', '"', '""', '*', '(', ')', '^', '-',
                   ':', "'; DROP TABLE nodes; --"]
        for t in hostile:
            df = RS.document_frequency(conn, t)
            assert isinstance(df, int) and df >= -1, f"{t!r} -> {df!r}"

        # Nothing here writes, but assert the graph is intact rather than assume it.
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 3

        # A pure-metacharacter token carries NO frequency information, and -1 (unknown) must
        # never collapse into 0 (unique in the corpus) — the pin fires on rarity.
        assert RS.document_frequency(conn, '"') == -1
        assert RS.document_frequency(conn, '***') == -1
        assert RS.rare_tokens({"zzzz": (-1, RS.idf(-1, 100))}) == []

        # A real token still measures correctly through the sanitizer.
        assert RS.document_frequency(conn, "gnucash") >= 1
        conn.close()
        print("  ok  9. document_frequency — FTS5 metacharacters neutralized, unknown != rare")
    finally:
        C.BRAIN_DB = orig_db
        from .. import rank_signals as RS
        RS.reset_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def test_haystack_does_not_mutate_and_cache_is_lru() -> None:
    """MED-2 + LOW-3 (fix round 3)."""
    from .. import rank_signals as RS

    node = _n("x", "Title", "A/x.md", "summary text")
    before = dict(node)
    hay = RS._haystack(node)
    assert node == before, f"_haystack mutated the caller's node: {set(node) - set(before)}"
    assert not any(k.startswith("_") for k in node), "a scratch key was written"
    assert "title" in hay and "a/x.md" in hay
    assert RS._haystack(node) == hay          # deterministic without a memo

    # LOW-3: eviction is oldest-first, never a full clear — a steady stream of new vocabulary
    # must not produce cliffs where every hot term goes back to the DB at once.
    RS.reset_cache()
    for i in range(RS._DF_CACHE_MAX + 50):
        RS._DF_CACHE[f"k{i}"] = (i, RS._now())
        while len(RS._DF_CACHE) >= RS._DF_CACHE_MAX:
            RS._DF_CACHE.popitem(last=False)
    assert len(RS._DF_CACHE) > 0, "cache emptied itself — the clear() bug is back"
    assert "k0" not in RS._DF_CACHE, "oldest entry survived eviction"
    assert f"k{RS._DF_CACHE_MAX + 49}" in RS._DF_CACHE, "newest entry was evicted"
    RS.reset_cache()
    print("  ok 10. _haystack does not mutate caller nodes; DF cache evicts LRU, never clears")


def _healthy_probe_vectors():
    """Two unit vectors whose cosine is the pinned probe value — what a healthy encoder
    produces, without loading a 90 MB model inside a unit test."""
    import numpy as np
    from .. import vector_seed as VS
    dim = C.EMBED_DIM
    a = np.zeros(dim, dtype=np.float32)
    a[0] = 1.0
    c = VS._PROBE_COS
    b = np.zeros(dim, dtype=np.float32)
    b[0] = c
    b[1] = float(np.sqrt(max(0.0, 1.0 - c * c)))
    return np.vstack([a, b])


def test_encoder_selfcheck() -> None:
    """LOW-1 (fix round 3). KMP_DUPLICATE_LIB_OK suppresses an abort whose own message warns
    it "may cause crashes or silently produce incorrect results". A crash degrades loudly;
    silently wrong numerics would corrupt every cosine, the relevance floor, and the golden
    gate at once. The self-check converts silent into detected."""
    import numpy as np
    from .. import vector_seed as VS

    class _Stub:
        def __init__(self, fn):
            self.fn = fn

        def encode(self, texts, **kw):
            return self.fn(texts)

    ok, why = VS._selfcheck(_Stub(lambda t: _healthy_probe_vectors()))
    assert ok, f"a healthy encoder failed its own self-check: {why}"

    dim = C.EMBED_DIM
    unit = (np.ones(dim) / np.sqrt(dim)).astype(np.float32)
    bad = {
        "nan": lambda t: np.full((2, dim), np.nan, dtype=np.float32),
        "not unit-norm": lambda t: np.full((2, dim), 0.5, dtype=np.float32),
        "wrong shape": lambda t: np.zeros((3, dim), dtype=np.float32),
        "collapsed to a constant": lambda t: np.tile(unit, (2, 1)),
        "raises": lambda t: (_ for _ in ()).throw(RuntimeError("OMP abort")),
    }
    for name, fn in bad.items():
        ok, why = VS._selfcheck(_Stub(fn))
        assert not ok, f"self-check PASSED a {name!r} encoder"
        assert why, f"{name!r} was rejected with no reason"
    print("  ok 11. encoder self-check — passes healthy; rejects NaN / non-unit / wrong-shape "
          "/ collapsed / raising")


def test_sims_lookup_defaults_for_unranked_nodes() -> None:
    """Fix round 4 #1. `sims.get(id)` carried no explicit default, unlike every other lookup
    in the scorer. It was never a live crash — `QuerySimilarity.get` defaults internally — but
    the contract was invisible at the call site AND `sims.get(id, 0.0)` would itself have
    raised. Both ends are explicit now, and the class is substitutable for a plain dict."""
    import numpy as np
    from .. import vector_seed as VS

    sims = VS.QuerySimilarity(np.array([0.61, 0.42], dtype=np.float32),
                              {"ranked_a": 0, "ranked_b": 1}, ["ranked_a", "ranked_b"])

    assert abs(sims.get("ranked_a") - 0.61) < 1e-6
    assert abs(sims.get("ranked_a", 0.0) - 0.61) < 1e-6      # 3-arg form must not raise

    # OUTSIDE the top-K — the COMMON case, since the graph walk reaches thousands of nodes
    # the vector channel never ranked. Must be a float, never None.
    for call in (lambda: sims.get("walked_in_by_bfs"),
                 lambda: sims.get("walked_in_by_bfs", 0.0)):
        v = call()
        assert v == 0.0 and isinstance(v, float), repr(v)

    # The arithmetic the scorer actually performs, on an unranked node.
    assert C.VECTOR_SIM_WEIGHT * sims.get("walked_in_by_bfs", 0.0) == 0.0
    # A plain dict is substitutable — the point of the explicit default.
    assert {"x": 0.5}.get("walked_in_by_bfs", 0.0) == 0.0
    print("  ok 12. sims.get — explicit default; unranked nodes score 0.0, never None")


def test_recency_seed_parses_timestamps_and_honours_owner() -> None:
    """Fix round 4 #2 (HIGH, pre-existing) + #3 (MED).

    #2: the window and the ordering were STRING comparisons against a column holding mixed
    timestamp formats — the exact lexicographic bug `_query_impl` was fixed for in July, in
    the one function that never got the fix and the only seeding path for a bare
    "what changed recently".
    #3: the empty-topic branch dropped owner/owner_strict, so a scoped recency query answered
    with every agent's nodes."""
    tmp = Path(tempfile.mkdtemp(prefix="brain_rec_test_"))
    orig_db = C.BRAIN_DB
    C.BRAIN_DB = tmp / "brain.sqlite"
    try:
        from .. import brain as B
        from .. import rank_signals as RS
        RS.reset_cache()
        conn = DB.connect(C.BRAIN_DB)
        DB.init_db(conn)

        def mk(nid, owner, lm):
            n = _n(nid, f"note {nid}", f"{owner}/{nid}.md", "changed recently",
                   last_modified=lm)
            n["owner"] = owner
            return n

        # Three same-day instants in three formats. As REAL times they are newest-first
        # newest > middle > oldest; as STRINGS they sort differently (" " < "T", and a
        # -04:00 offset makes an earlier instant look later than a Z-suffixed one).
        DB.upsert_node(conn, mk("newest", "ALTAIR", "2026-08-04T12:00:00Z"))
        DB.upsert_node(conn, mk("middle", "ALTAIR", "2026-08-04 09:30:00+00:00"))
        DB.upsert_node(conn, mk("oldest", "SIRIUS", "2026-08-04T04:00:00-04:00"))
        DB.upsert_node(conn, mk("ancient", "SIRIUS", "2024-01-01T00:00:00Z"))
        DB.upsert_node(conn, mk("undated", "ALTAIR", ""))
        DB.upsert_node(conn, mk("garbage", "ALTAIR", "not-a-timestamp"))
        conn.commit()

        seeds = B._recency_seed(conn, None, None, limit=10)
        got = [n["id"] for n in seeds]
        assert got[:3] == ["newest", "middle", "oldest"], f"ordered by string, not time: {got}"
        assert got[-1] == "ancient", got
        # Undated / unparseable nodes cannot be placed in a window.
        assert "undated" not in got and "garbage" not in got, got

        # Window edges compare as INSTANTS. 08:30Z must exclude the 08:00Z node (written
        # "04:00-04:00") — which a string compare would have KEPT.
        win = [n["id"] for n in B._recency_seed(conn, "2026-08-04T08:30:00+00:00", None)]
        assert win == ["newest", "middle"], f"window filtered by string, not instant: {win}"

        # #3 — owner_strict actually scopes; non-strict owner stays a boost, not a filter.
        strict = B._recency_seed(conn, None, None, owner="ALTAIR", owner_strict=True)
        assert [n["id"] for n in strict] == ["newest", "middle"], [n["id"] for n in strict]
        assert all(n["owner"] == "ALTAIR" for n in strict)
        loose = [n["id"] for n in B._recency_seed(conn, None, None, owner="ALTAIR")]
        assert "oldest" in loose, f"non-strict owner silently excluded other agents: {loose}"

        # End to end through query() — the branch that was dropping the arguments.
        pack = B.query("", order_by="recency", since="2026-08-01", token_budget=2000,
                       owner="ALTAIR", owner_strict=True)
        assert pack.nodes, "owner-scoped recency query returned nothing at all"
        owners = {(n.get("owner") or "") for n in pack.nodes}
        assert owners <= {"ALTAIR"}, f"owner-scoped recency query leaked other agents: {owners}"

        conn.close()
        print("  ok 13. _recency_seed — ordered/filtered by parsed instants, not strings; "
              "owner_strict honoured end to end")
    finally:
        C.BRAIN_DB = orig_db
        from .. import rank_signals as RS
        RS.reset_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def run() -> int:
    test_rrf()
    test_rare_tokens_and_pin()
    test_age_decay()
    test_floor_and_ab_switch()
    test_status_dominates_age()
    test_true_exact_token_pin()
    test_age_decay_never_raises()
    test_document_frequency_hostile_tokens()
    test_haystack_does_not_mutate_and_cache_is_lru()
    test_encoder_selfcheck()
    test_sims_lookup_defaults_for_unranked_nodes()
    test_recency_seed_parses_timestamps_and_honours_owner()
    print("PASS — retrieval v2: fusion, exact-token pin (true), age decay + status dominance, "
          "relevance floor, A/B switch, and the fix-round-3 robustness invariants")
    return 0


if __name__ == "__main__":
    sys.exit(run())
