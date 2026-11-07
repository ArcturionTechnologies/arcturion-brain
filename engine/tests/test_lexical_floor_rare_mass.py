"""Hermetic unit tests for the guard floor refinement (Leg 3 / 5b, retrieval-precision
2026-08-07).

Leg 5 left the lexical half of the two-floor decision unconditioned: `lexical_coverage >=
LEXICAL_FLOOR` was sufficient ALONE, full stop. Measured 2026-08-07, six of the eleven golden
negatives still returned confident packs, every one of them through exactly that branch, via
two distinct mechanisms:

  1. COVERAGE ASSEMBLED FROM THE COMMON REMAINDER. `lexical_coverage` is IDF-weighted but
     normalized by the query's WHOLE mass, so a document matching only the low-IDF words of a
     short question clears 0.42 while missing the word that makes it a question — neg-03 "how
     many goats does the founder own" scored 0.472 on {many, founder, own} with `goats` (53%
     of the query's mass) absent. `rare_mass_coverage` + `FLOOR_RARE_COVERAGE_MIN` closes it.
  2. VOCABULARY COLLISION. A document can hold ALL of the query's rare terms and be about
     something else — neg-07's governance restructure doc matched evacuation/route/office at
     cosine 0.090. `FLOOR_LEX_ALONE_MIN_SIM` closes it.

Both gates only ever REMOVE a lexical-alone pass. Neither can make a pack pass that failed
before, so neither can turn a refusal back into a confident wrong answer.

Covers:
  1. rare-mass coverage is scale-free — the neg-03 shape is refused, the answer shape passes
  2. GUARANTEED FALLBACK — an all-common-word query (no rare mass to measure) falls back to
     plain `lexical_coverage` rather than becoming unreachable
  3. semantic sanity gate — full rare coverage at an unrelated cosine is refused
  4. DEGRADED SIGNAL — a node with no real cosine SKIPS the sanity gate rather than failing it
     (both `sims=None` and a node missing from a live `sims`)
  5. the other two branches are untouched — semantic-alone and the corroboration band decide
     exactly as they did under Leg 5
  6. both flags OFF reproduces the Leg 5 decision exactly
  7. monotonicity — the refinement can only ever remove a pass, never add one

Run:  python3 -m engine.tests.test_lexical_floor_rare_mass
"""
from __future__ import annotations

import sys

from .. import config as C

# Terms are (document_frequency, idf). Only the idf half is read by the floor.
# The neg-03 shape: one rare term carrying most of the mass, three common ones.
GOATS = {"goats": (6, 9.05), "many": (965, 4.12), "founder": (12254, 1.58), "own": (5494, 2.38)}
# An all-common-word query: nothing at or above FLOOR_RARE_TERM_MIN_IDF.
COMMON_ONLY = {"how": (30000, 0.61), "the": (40000, 0.32), "own": (5494, 2.38)}


def _n(nid, text):
    """A node whose haystack is exactly `text` (id+title+summary+body_excerpt+path)."""
    return {"id": nid, "title": text, "summary": "", "body_excerpt": "", "path": ""}


def _floor_env(**over):
    """Pin every number the floor reads to its production default, plus overrides."""
    keys = ("RELEVANCE_FLOOR", "LEXICAL_FLOOR", "SEMANTIC_FLOOR", "CORROBORATION_SIM",
            "CORROBORATION_LEX_MIN", "LEXICAL_FLOOR_RARE_MASS", "FLOOR_RARE_TERM_MIN_IDF",
            "FLOOR_RARE_COVERAGE_MIN", "LEXICAL_FLOOR_SIM_SANITY", "FLOOR_LEX_ALONE_MIN_SIM")
    orig = {k: getattr(C, k) for k in keys}
    defaults = {"RELEVANCE_FLOOR": 0.42, "LEXICAL_FLOOR": 0.42, "SEMANTIC_FLOOR": 0.60,
                "CORROBORATION_SIM": 0.45, "CORROBORATION_LEX_MIN": 0.30,
                "LEXICAL_FLOOR_RARE_MASS": True, "FLOOR_RARE_TERM_MIN_IDF": 3.0,
                "FLOOR_RARE_COVERAGE_MIN": 0.90, "LEXICAL_FLOOR_SIM_SANITY": True,
                "FLOOR_LEX_ALONE_MIN_SIM": 0.20}
    defaults.update(over)
    for k, v in defaults.items():
        setattr(C, k, v)
    return orig


def _restore(orig):
    for k, v in orig.items():
        setattr(C, k, v)


# ------------------------------------------------------------------ 1. rare-mass coverage

def test_rare_mass_coverage_is_scale_free() -> None:
    from .. import rank_signals as RS

    orig = _floor_env()
    try:
        # The neg-03 shape: matches every common word, misses the one that matters.
        collide = _n("founder-hq", "founder how many things we own")
        assert abs(RS.lexical_coverage(collide, GOATS) - 0.472) < 0.01, \
            RS.lexical_coverage(collide, GOATS)
        # Plain coverage clears LEXICAL_FLOOR (0.472 >= 0.42) — this is precisely why the
        # unconditioned Leg 5 branch let it through.
        assert RS.lexical_coverage(collide, GOATS) >= C.LEXICAL_FLOOR
        # Rare-mass coverage tells the truth: `many` alone out of {goats, many}.
        assert abs(RS.rare_mass_coverage(collide, GOATS) - 4.12 / (9.05 + 4.12)) < 1e-9
        assert RS.rare_mass_coverage(collide, GOATS) < C.FLOOR_RARE_COVERAGE_MIN
        # ...and a sim high enough to have passed the old branch does NOT rescue it.
        assert RS.node_passes_two_floor(collide, GOATS, sim=0.42) is False

        # An actual answer holds the distinguishing vocabulary.
        answer = _n("herd", "how many goats the founder owns: many")
        assert RS.rare_mass_coverage(answer, GOATS) == 1.0
        assert RS.node_passes_two_floor(answer, GOATS, sim=0.42) is True
        print("  ok  1. rare-mass coverage — neg-03 shape refused (0.313 < 0.90), "
              "answer shape passes (1.000)")
    finally:
        _restore(orig)


def test_guaranteed_fallback_when_query_has_no_rare_mass() -> None:
    """A question made entirely of common words has no rare mass to measure coverage OF.
    The gate must fall back to plain coverage, not read "nothing to measure" as "no
    evidence" — that would make the lexical floor permanently unreachable for the class."""
    from .. import rank_signals as RS

    orig = _floor_env()
    try:
        assert all(i < C.FLOOR_RARE_TERM_MIN_IDF for _df, i in COMMON_ONLY.values())
        node = _n("cov", "how the own")
        assert RS.lexical_coverage(node, COMMON_ONLY) == 1.0
        assert RS.rare_mass_coverage(node, COMMON_ONLY) == 1.0     # == plain coverage
        assert RS.node_passes_two_floor(node, COMMON_ONLY, sim=0.42) is True

        partial = _n("part", "own")
        assert RS.rare_mass_coverage(partial, COMMON_ONLY) == \
            RS.lexical_coverage(partial, COMMON_ONLY)
        print("  ok  2. guaranteed fallback — no rare mass in the query falls back to plain "
              "lexical_coverage, floor stays reachable")
    finally:
        _restore(orig)


# ------------------------------------------------------------------ 3/4. semantic sanity gate

def test_sanity_gate_refuses_vocabulary_collision() -> None:
    """The neg-07 shape: every rare query term present, cosine 0.09 — the words co-occur in a
    document about an unrelated subject."""
    from .. import rank_signals as RS

    orig = _floor_env()
    try:
        node = _n("restructure", "goats many founder own")
        assert RS.lexical_coverage(node, GOATS) == 1.0
        assert RS.rare_mass_coverage(node, GOATS) == 1.0
        # Below the sanity bar: refused despite perfect lexical evidence.
        assert RS.node_passes_two_floor(node, GOATS, sim=0.09) is False
        # At/above it: the lexical-alone branch stands, exactly as under Leg 5.
        assert RS.node_passes_two_floor(node, GOATS, sim=0.20) is True
        # A pasted-error-string cosine (mediocre, not unrelated) is unharmed — this is the
        # whole reason the lexical-alone branch exists, and ex-10 lives here at 0.316.
        assert RS.node_passes_two_floor(node, GOATS, sim=0.316) is True
        print("  ok  3. sanity gate — full rare coverage at cosine 0.09 refused, "
              "mediocre-but-real cosine (0.316, the ex-10 shape) unharmed")
    finally:
        _restore(orig)


def test_degraded_vector_signal_skips_the_sanity_gate() -> None:
    """A missing signal must cost its own contribution, never the request.

    `vector_seed.similarity` returns None outright while the encoder loads, and a node with no
    embedding has no cosine at all. Reading either as "semantically unrelated" would refuse
    every pack in the fleet the moment the vector channel degraded."""
    from .. import rank_signals as RS

    orig = _floor_env()
    try:
        node = _n("hit", "goats many founder own")
        head = [node]

        # a) sims=None (encoder loading / vectors unusable) — floor still passes on lexical.
        ok, _conf = RS.pack_passes_floor(head, GOATS, sims=None)
        assert ok is True, "degraded vector channel must not empty a lexically-certain pack"

        # b) sims present but this node has no embedding — same reasoning, per node.
        ok, _conf = RS.pack_passes_floor(head, GOATS, sims={"someone-else": 0.9})
        assert ok is True, "a node absent from sims must not be read as cosine 0.0"

        # c) sims present AND this node measured as unrelated — the gate bites.
        ok, _conf = RS.pack_passes_floor(head, GOATS, sims={"hit": 0.09})
        assert ok is False, "a MEASURED unrelated cosine must fail the sanity gate"

        # The explicit-parameter form carries the same contract.
        assert RS.node_passes_two_floor(node, GOATS, 0.09, sim_trusted=False) is True
        assert RS.node_passes_two_floor(node, GOATS, 0.09, sim_trusted=True) is False
        print("  ok  4. degraded signal — sims=None and unembedded nodes SKIP the sanity "
              "gate; only a measured cosine can fail it")
    finally:
        _restore(orig)


# ------------------------------------------------------------------ 5/6/7. containment

def test_other_two_branches_untouched() -> None:
    """The refinement narrows the LEXICAL branch only. Semantic-alone and the corroboration
    band must decide exactly as they did under Leg 5 — including for nodes the new gates
    would refuse on the lexical branch."""
    from .. import rank_signals as RS

    orig = _floor_env()
    try:
        # The neg-03 collider: fails the lexical branch under the refinement...
        collide = _n("founder-hq", "founder how many things we own")
        assert RS.rare_mass_coverage(collide, GOATS) < C.FLOOR_RARE_COVERAGE_MIN
        # ...but semantic-alone still carries it at a real paraphrase cosine.
        assert RS.node_passes_two_floor(collide, GOATS, sim=0.62) is True
        # ...and so does the corroboration band (sim >= 0.45 with coverage >= 0.30).
        assert RS.lexical_coverage(collide, GOATS) >= C.CORROBORATION_LEX_MIN
        assert RS.node_passes_two_floor(collide, GOATS, sim=0.46) is True
        # A weak sim with only incidental coverage is still refused, as under Leg 5.
        thin = _n("thin", "founder")
        assert RS.node_passes_two_floor(thin, GOATS, sim=0.46) is False
        print("  ok  5. semantic-alone and corroboration band untouched by the refinement")
    finally:
        _restore(orig)


def test_flags_off_reproduce_leg5_decision() -> None:
    """Both flags OFF must be the Leg 5 decision exactly — the A/B restore this leg ships
    behind. Checked on the shapes the refinement is designed to change."""
    from .. import rank_signals as RS

    orig = _floor_env(LEXICAL_FLOOR_RARE_MASS=False, LEXICAL_FLOOR_SIM_SANITY=False)
    try:
        collide = _n("founder-hq", "founder how many things we own")
        collision = _n("restructure", "goats many founder own")
        # Leg 5: coverage >= LEXICAL_FLOOR is sufficient ALONE at ANY sim, including 0.0.
        assert RS.node_passes_two_floor(collide, GOATS, sim=0.0) is True
        assert RS.node_passes_two_floor(collision, GOATS, sim=0.0) is True
        ok, _c = RS.pack_passes_floor([collision], GOATS, sims={"restructure": 0.09})
        assert ok is True
        print("  ok  6. both flags OFF — Leg 5 lexical-alone sufficiency restored exactly")
    finally:
        _restore(orig)


def test_refinement_is_monotone_can_only_remove_a_pass() -> None:
    """The safety property the whole leg rests on: the gates are conjunctive additions to ONE
    branch, so for every input, refinement-ON passing implies Leg-5 passing. A refinement that
    could ADD a pass could turn an honest refusal back into a confident wrong answer."""
    from .. import rank_signals as RS

    nodes = [_n("a", "goats many founder own"), _n("b", "founder how many things we own"),
             _n("c", "founder"), _n("d", "goats"), _n("e", ""), _n("f", "many own")]
    added = 0
    for node in nodes:
        for stats in (GOATS, COMMON_ONLY, {}):
            for sim in (0.0, 0.09, 0.20, 0.31, 0.44, 0.46, 0.59, 0.61, 0.95):
                orig = _floor_env(LEXICAL_FLOOR_RARE_MASS=False,
                                  LEXICAL_FLOOR_SIM_SANITY=False)
                try:
                    leg5 = RS.node_passes_two_floor(node, stats, sim)
                finally:
                    _restore(orig)
                orig = _floor_env()
                try:
                    refined = RS.node_passes_two_floor(node, stats, sim)
                finally:
                    _restore(orig)
                assert not (refined and not leg5), \
                    f"refinement ADDED a pass: {node['id']} sim={sim} stats={list(stats)}"
                added += 1
    print(f"  ok  7. monotone over {added} (node, stats, sim) combinations — "
          "the refinement can only ever remove a lexical-alone pass")


def run() -> int:
    test_rare_mass_coverage_is_scale_free()
    test_guaranteed_fallback_when_query_has_no_rare_mass()
    test_sanity_gate_refuses_vocabulary_collision()
    test_degraded_vector_signal_skips_the_sanity_gate()
    test_other_two_branches_untouched()
    test_flags_off_reproduce_leg5_decision()
    test_refinement_is_monotone_can_only_remove_a_pass()
    print("PASS — lexical floor rare-mass + sanity refinement: scale-free rare coverage, "
          "guaranteed fallback, collision refusal, degraded-signal safety, other branches "
          "untouched, flags-off restore, monotonicity")
    return 0


if __name__ == "__main__":
    sys.exit(run())
