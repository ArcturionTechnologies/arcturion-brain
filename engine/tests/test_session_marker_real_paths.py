"""Synthetic regression checks for raw transcripts and authored summaries.

The path shapes exercise historical and structural detection without real session data.
"""
from __future__ import annotations

import sys

from .. import brain as B
from .. import util as U


# Invented examples retain the directory and extension shapes needed by the checks.
RAW = "EXAMPLE/Memory/Sessions/raw/2025-01-02-example.jsonl"
DISTILLED = "EXAMPLE/Memory/Sessions/distilled/2025-01-02-example-summary.md"
AUTHORED = "EXAMPLE/Knowledge/Methods/example-index-hygiene.md"
LEGACY = "example/wiki/logs/sessions/2025-01-02-example.jsonl"
CATCHALL_RAW = "ARCTURION_Brain/memory/0608_SESSION_DISTILLATIONS/2025-01-02-example.jsonl"
CATCHALL_RAW_EDIT = ("ARCTURION_Brain/memory/0608_SESSION_DISTILLATIONS/_raw_sessions/"
                     "2025-01-02-example.jsonl#example.py")
CATCHALL_AUTHORED = "ARCTURION_Brain/memory/0608_SESSION_DISTILLATIONS/claude/example-summary.md"

_FAILS: list[str] = []


def _check(label: str, got, want) -> None:
    if got != want:
        _FAILS.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  ✗ {label}: got {got!r}, want {want!r}")
    else:
        print(f"  ✓ {label}")


def _node(path: str, title: str = "session notes on brain index hygiene"):
    return {"id": "n", "type": "Source", "family": "document", "title": title,
            "path": path, "owner": "ALTAIR", "status": "active", "governance_tier": 0,
            "summary": "", "body_excerpt": "", "centrality": 0.0,
            "last_modified": "2026-07-30T00:00:00Z", "metadata": {}}


def _penalty_applied(path: str) -> bool:
    """Score the same node with and without the session-log path; the gap is the penalty."""
    scored = B._relevance_score(_node(path), "brain index hygiene")
    neutral = B._relevance_score(_node(AUTHORED), "brain index hygiene")
    return scored < neutral


def run() -> int:
    print("session-transcript penalty, synthetic path shapes")

    # The bug, stated as an assertion: the marker must match where transcripts LIVE.
    _check("penalty fires on Memory/Sessions/raw/", _penalty_applied(RAW), True)

    # The deliberate carve-out: authored distillations are knowledge, not prompt-echoes.
    _check("penalty does NOT fire on Memory/Sessions/distilled/",
           _penalty_applied(DISTILLED), False)

    # The legacy location must keep working — test_session_log_demotion.py relies on it,
    # and old material may still be re-indexed from backups.
    _check("penalty still fires on legacy wiki/logs/sessions/",
           _penalty_applied(LEGACY), True)

    # A plain authored path is never penalized.
    _check("no penalty on authored Knowledge/ path", _penalty_applied(AUTHORED), False)

    # Leg 4: the shelf the location markers could not reach. Both `.jsonl` shapes are raw
    # transcripts; the `.md` beside them on the SAME shelf is authored and must survive.
    _check("penalty fires on the 0608 catch-all transcript",
           _penalty_applied(CATCHALL_RAW), True)
    _check("penalty fires on a 0608 Edit node (transcript.jsonl#file)",
           _penalty_applied(CATCHALL_RAW_EDIT), True)
    _check("penalty does NOT fire on the 0608 authored .md distillation",
           _penalty_applied(CATCHALL_AUTHORED), False)

    # Guard the size of the effect, not just its direction: a raw transcript whose title
    # is a verbatim echo of the prompt must still lose to authored knowledge whose title
    # matches less well. That inversion is the entire point of the -14.
    echo = B._relevance_score(_node(RAW, "brain index hygiene"), "brain index hygiene")
    doctrine = B._relevance_score(
        _node(AUTHORED, "method: brain index hygiene deleting nodes safely"),
        "brain index hygiene")
    _check("authored doctrine outranks the prompt-echo transcript", doctrine > echo, True)

    print()
    if _FAILS:
        print(f"\U0001F6D1 FAIL — {len(_FAILS)} assertion(s)")
        for f in _FAILS:
            print(f"   - {f}")
        return 1
    print("✅ PASS — session-transcript penalty fires on synthetic path shapes")
    return 0


# ── pytest surface (Leg 4) ───────────────────────────────────────────────────
# The standalone `run()` above pre-dates pytest in this package and was therefore never
# collected by `python3 -m pytest engine/tests/` — a guard nobody runs is not a guard, and
# this file's whole subject is a guard that silently stopped applying. It is wrapped below
# rather than rewritten, so the runner keeps working unchanged.

def test_penalty_fires_on_every_live_path_shape() -> None:
    _FAILS.clear()
    assert run() == 0, f"real-path assertions failed: {_FAILS}"


def test_transcripts_are_identified_structurally_not_by_location() -> None:
    """The mechanism, not the two known folders: a transcript filed ANYWHERE is raw.

    This is the assertion the previous two versions of this guard could not make. Both
    tested "does the marker match this string", which is true of any string the marker was
    written from. A transcript dropped in a folder no one has thought of yet must still be
    demoted, because that is precisely how this broke — twice."""
    unheard_of = "SOME_FUTURE_SHELF/nobody/planned/this/2025-01-02-example.jsonl"
    assert U.is_raw_transcript_path(unheard_of)
    assert _penalty_applied(unheard_of)

    # The node's TYPE is sufficient on its own — the harvester mints Session/Edit only from
    # a transcript, and no restructure can move a type.
    assert U.is_raw_transcript_node({"type": "Session", "path": "anywhere/at/all.md"})
    assert U.is_raw_transcript_node({"type": "Edit", "path": ""})
    assert not U.is_raw_transcript_node({"type": "Source", "path": AUTHORED})


def test_authored_distillations_stay_unpenalized_on_both_shelves() -> None:
    """The carve-out is the load-bearing half. Widening the rule to reach the 0608 shelf
    must NOT sweep up the authored `.md` sitting next to the transcripts. Whatever else changes, these two must stay False."""
    for path in (DISTILLED, CATCHALL_AUTHORED, AUTHORED):
        assert not U.is_raw_transcript_path(path), path
        assert not _penalty_applied(path), path


def test_semantic_term_is_withheld_from_every_transcript() -> None:
    """The vector-side twin. It read a location list of its own
    (`config.VECTOR_SEED_EXCLUDE_PATHS`) and so covered a DIFFERENT subset of transcripts
    than the penalty did — two rules for one question, which is the drift this codebase
    keeps paying for. Both now route through the same predicate."""
    for path in (RAW, LEGACY, CATCHALL_RAW, CATCHALL_RAW_EDIT):
        assert B._semantically_untrustworthy(_node(path)), path
    for path in (DISTILLED, CATCHALL_AUTHORED, AUTHORED):
        assert not B._semantically_untrustworthy(_node(path)), path


def test_empty_and_missing_paths_do_not_crash_or_match() -> None:
    """Fail-open on absent data: a node with no path is not a transcript."""
    assert not U.is_raw_transcript_path(None)
    assert not U.is_raw_transcript_path("")
    assert not U.is_raw_transcript_node({"type": "Source"})


if __name__ == "__main__":
    sys.exit(run())
