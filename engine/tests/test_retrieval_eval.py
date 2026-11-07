"""Hermetic tests for the retrieval ruler (Leg 1, retrieval-honesty 2026-08-04, no pytest).

The thing being guarded here is subtle: a scorecard that cannot fail. The previous one
(`engine/eval.py`) reported 6/6 forever because the Brain indexed the file that held its
own golden questions, so the eval was the top-1 answer to four of its own tests. Two
properties have to hold together, and either alone is insufficient:

  A. **The fixtures are not in the corpus** — `config.is_brain_fixture_path` prunes them
     from the walk and `maintenance.skiplist_gc` deletes any that already landed.
  B. **Scoring is on node IDs** — because de-indexing is a rule someone can quietly relax,
     while a node-ID assertion fails loudly the moment the wrong document wins.

Run:  python3 -m engine.tests.test_retrieval_eval
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from .. import config as C
from .. import retrieval_eval as E
from ..maintenance.skiplist_gc import classify
from ..walker import walk_brain


def _node(nid, path="ALTAIR/x.md", status="active", title="t"):
    return {"id": nid, "path": path, "status": status, "title": title, "summary": ""}


def _fail(msg: str) -> None:
    raise AssertionError(msg)


# --------------------------------------------------------------- A. de-indexing holds

def test_fixture_paths_are_recognised() -> None:
    for p in ["engine/eval.py", "ARCTURION_Brain/engine/eval.py",
              "engine/retrieval_eval.py", "ARCTURION_Brain/engine/retrieval_eval.py",
              "engine/tests/test_retrieval_eval.py",
              "engine/tests/fixtures/golden.jsonl",
              "ARCTURION_Brain/engine/tests/fixtures/golden.jsonl",
              "/engine/tests/fixtures/baseline.json"]:
        assert C.is_brain_fixture_path(p), f"fixture not recognised: {p}"

    # Production engine source stays indexed — the Brain SHOULD know its own pipeline.
    # Over-pruning would be its own defect ("who owns the brain reindex pipeline" is a
    # real question with a real answer in engine/reindex.py).
    for p in ["engine/brain.py", "engine/reindex.py", "ARCTURION_Brain/engine/walker.py",
              "ARCTURION_Brain/canon/0007_ONTOLOGY.md"]:
        assert not C.is_brain_fixture_path(p), f"over-pruned: {p}"

    # Another agent's engine/tests is NOT this Brain's fixture set. A bare
    # "engine/tests/" skip fragment would have de-indexed 57 legitimate VEGA nodes.
    for p in ["VEGA/77 🛠 Back Office/Scripts/engine/tests/test_allocator_ramp.py",
              "SIRIUS/engine/tests/x.py"]:
        assert not C.is_brain_fixture_path(p), f"collateral de-index: {p}"

    # Boundary. A bare `startswith` on a FILE fragment matched neighbours too, which is
    # wrong in both directions: it silently de-indexed files nobody asked to prune, and a
    # real file named that way would have been invisible with no error anywhere.
    for p in ["engine/eval.py2", "engine/eval.pyc", "engine/eval.py.bak",
              "engine/retrieval_eval.pyc", "engine/tests_helpers/x.py",
              "engine/testsuite.py"]:
        assert not C.is_brain_fixture_path(p), f"prefix over-match: {p}"
    # …while the directory fragment still matches the bare directory and everything under it.
    assert C.is_brain_fixture_path("engine/tests")
    assert C.is_brain_fixture_path("engine/tests/deep/nested/x.py")


def test_reindex_overlap_is_detected_and_skips_scoring() -> None:
    """03:30 is a fixed offset from a 02:30 walk with no completion signal. An overrunning
    walk means the gate scores a HALF-REBUILT graph and publishes that as the nightly
    verdict — recall craters, it FAILs, and it pages COVENANT about a ranker that never
    changed."""
    orig = E.reindex_in_progress
    tmp = Path(tempfile.mkdtemp(prefix="overlap_"))
    try:
        report = tmp / "report.md"
        report.write_text("PREVIOUS REAL SCORECARD")
        orig_report = E.REPORT_PATH
        E.REPORT_PATH = report

        E.reindex_in_progress = lambda: (True, "test: walker still running")
        code = E.main(["--no-alert"])
        assert code == 0, f"a scheduling overlap is not a failure, got exit {code}"
        # The previous scorecard is the most recent MEASUREMENT and must survive: replacing
        # it with a "didn't score" placeholder destroys numbers to record a non-event.
        assert report.read_text() == "PREVIOUS REAL SCORECARD", "skip overwrote the report"

        # --force overrides, for a human who knows better.
        code = E.main(["--no-alert", "--force", "--token-budget", "300"])
        assert code in (0, 1), code
        assert report.read_text() != "PREVIOUS REAL SCORECARD", "--force did not score"
    finally:
        E.reindex_in_progress = orig
        E.REPORT_PATH = orig_report
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_reindex_detector_ignores_the_shared_ingest_lock() -> None:
    """The advisory lock is SHARED with brain-ingest.sh, which takes it every ~20 s. Treating
    its mere presence as "reindex in progress" would skip the eval essentially every night —
    a gate that never runs, which is the exact failure this leg exists to close."""
    tmp = Path(tempfile.mkdtemp(prefix="lock_"))
    orig_lock, orig_job = E.REINDEX_LOCK, E.REINDEX_JOB
    try:
        E.REINDEX_JOB = "com.arcturion.definitely-not-a-real-job"   # force signal 2 to miss
        lock = tmp / "brain-write.lock"
        E.REINDEX_LOCK = lock

        assert E.reindex_in_progress()[0] is False, "no lock file → not in progress"

        # Our own PID holds it, and this process is a test runner, not a reindex.
        lock.write_text(f"{os.getpid()}\n")
        busy, why = E.reindex_in_progress()
        assert busy is False, f"a non-reindex lock holder was misread as a reindex: {why}"

        # A dead PID must not read as in-progress either (the lock is not removed if a
        # reindex is killed, so a stale file would otherwise skip the eval forever).
        lock.write_text("999999\n")
        assert E.reindex_in_progress()[0] is False, "stale lock treated as live"

        # Garbage in the lock file fails open rather than raising.
        lock.write_text("not-a-pid\n")
        assert E.reindex_in_progress()[0] is False
    finally:
        E.REINDEX_LOCK, E.REINDEX_JOB = orig_lock, orig_job
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_walk_brain_never_yields_a_fixture() -> None:
    """The live walk itself — not a predicate about it — must not emit a fixture."""
    offenders = []
    for path, _family, _ntype in walk_brain():
        try:
            rel = path.relative_to(C.BRAIN_ROOT)
        except ValueError:
            continue
        if C.is_brain_fixture_path(rel):
            offenders.append(str(rel))
    assert not offenders, f"walk_brain yielded eval fixtures: {offenders[:5]}"


def test_gc_classifies_the_three_junk_classes() -> None:
    assert classify("ARCTURION_Brain/engine/eval.py") == "brain_fixture"
    assert classify("ARCTURION_Brain/engine/tests/fixtures/golden.jsonl") == "brain_fixture"
    assert classify("05 🔌 TOOLS/clis/.claude/worktrees/x/00 GOV/README.md") == "worktree_twin"
    assert classify("ALTAIR/03 Tools/Runbooks/pv/.deps/_pytest/__init__.py") == "vendored_dep"
    assert classify("x/node_modules/pkg/index.js") == "vendored_dep"
    # Kept: the deliberately-indexed surfaces. `.claude/plans` and `.claude/hooks` are walk
    # roots (walker._EXTRA_ROOTS) whose stored paths start with a dot — a blanket
    # should_skip_path sweep would delete all 129 of them.
    for keep in [".claude/plans/covenant-audit.md", ".claude/hooks/session-weight-nudge.py",
                 "ARCTURION_EXTERNAL_RUNTIME/SERVICE_CATALOG.md",
                 "ALTAIR/000 🤖 Altair/Memory/Decisions/x.md",
                 "VEGA/77 🛠 Back Office/Scripts/engine/tests/test_x.py"]:
        assert classify(keep) is None, f"GC would wrongly sweep: {keep}"


def test_is_fixture_node() -> None:
    assert E.is_fixture_node(_node("mem_x", "ARCTURION_Brain/engine/eval.py"))
    from unittest.mock import patch
    with patch.object(E, "EVAL_ARTIFACT_NODE_IDS", {"invented-fixture-id"}):
        assert E.is_fixture_node(_node("invented-fixture-id", ""))
    assert not E.is_fixture_node(_node("mem_x", "ALTAIR/000 🤖 Altair/Memory/Decisions/x.md"))
    assert not E.is_fixture_node(None)


# --------------------------------------------------------------- B. the golden set

def test_golden_set_shape_and_coverage() -> None:
    entries = E.load_golden()
    assert len(entries) >= 20, f"golden set too small to be a ruler: {len(entries)}"
    by_class: dict[str, int] = {}
    for e in entries:
        by_class[e["class"]] = by_class.get(e["class"], 0) + 1
    # Every failure class measured on 2026-08-04 must be represented. A class that drops
    # to zero is a blind spot that no aggregate number would reveal.
    for cls in E.VALID_CLASSES:
        assert by_class.get(cls, 0) >= 2, f"class {cls!r} has {by_class.get(cls, 0)} entries"
    assert by_class["negative"] >= 4, "too few negative controls — 'always answers' is a defect"
    # ids are stable and referable
    for e in entries:
        assert e["id"].replace("-", "").isalnum(), f"unstable id: {e['id']}"


def test_golden_expected_ids_are_not_fixtures() -> None:
    """An expected answer that is itself a fixture would re-create the original bug."""
    for e in E.load_golden():
        for nid in (e.get("expect") or []) + (e.get("alternates") or []):
            assert not nid.startswith("mem_eval_py_"), f"{e['id']} expects a fixture: {nid}"


def _write_golden(tmp: Path, entries: list[dict]) -> Path:
    p = tmp / "golden.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return p


def test_load_golden_rejects_malformed() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="golden_test_"))
    ok = {"id": "a-01", "class": "paraphrase", "q": "q", "expect": ["n1"], "note": "n"}
    bad_cases = [
        ([{**ok, "class": "vibes"}], "unknown class"),
        ([{**ok, "expect": []}], "positive entry with no expected node IDs"),
        ([{k: v for k, v in ok.items() if k != "note"}], "missing note"),
        ([ok, {**ok, "q": "other"}], "duplicate id"),
        ([], "empty set"),
    ]
    for entries, label in bad_cases:
        p = _write_golden(tmp, entries)
        try:
            E.load_golden(p)
        except ValueError:
            continue
        _fail(f"load_golden accepted a malformed set: {label}")
    # …and accepts a well-formed one
    assert len(E.load_golden(_write_golden(tmp, [ok]))) == 1
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------- C. scoring semantics

def test_score_positive_entry() -> None:
    e = {"id": "p-1", "class": "paraphrase", "q": "?", "expect": ["good"],
         "alternates": ["ok"], "note": "n"}
    r = E.score_entry(e, [_node("good"), _node("junk")])
    assert r["top1"] and r["recall5"] and r["passed"]

    # An alternate deep in the pack is recall, not top1 — the distinction the old
    # substring scorer could not make.
    r = E.score_entry(e, [_node("junk"), _node("junk2"), _node("ok")])
    assert not r["top1"] and r["recall5"] and r["passed"]

    # Nothing relevant → fail, with the wrong winner named.
    r = E.score_entry(e, [_node("junk")])
    assert not r["passed"] and "junk" in r["reason"]

    # Beyond depth 5 does not count.
    r = E.score_entry(e, [_node(f"j{i}") for i in range(5)] + [_node("good")])
    assert not r["recall5"] and not r["passed"]

    # A document merely *containing the right word* is no longer a pass: the old
    # `_on_topic` matched title+summary substrings, so this shape scored green.
    r = E.score_entry(e, [_node("wrong_node", title="all about the good thing")])
    assert not r["passed"], "substring-shaped pass leaked back in"


def test_score_recency_entry_rejects_superseded() -> None:
    e = {"id": "r-1", "class": "recency", "q": "?", "expect": ["current"],
         "unexpected": ["old"], "note": "n"}
    assert E.score_entry(e, [_node("current")])["passed"]
    r = E.score_entry(e, [_node("current"), _node("old")])
    assert not r["passed"] and r["superseded_hits"] == ["old"], \
        "a superseded twin in the top 5 must fail even when the current answer is #1"


def test_score_negative_and_contamination() -> None:
    neg = {"id": "n-1", "class": "negative", "q": "?", "expect": [], "note": "n"}
    assert E.score_entry(neg, [])["passed"]
    assert not E.score_entry(neg, [_node("anything")])["passed"], \
        "answering a question with no answer must fail"

    con = {"id": "c-1", "class": "contamination", "q": "?", "expect": [],
           "forbid_fixture": True, "note": "n"}
    assert E.score_entry(con, [_node("mem_ok", "ALTAIR/a.md")])["passed"]
    r = E.score_entry(con, [_node("mem_x", "ARCTURION_Brain/engine/eval.py")])
    assert not r["passed"] and r["fixture_hits"] == ["mem_x"]


def test_run_aggregates_per_class() -> None:
    """A blended number hides which mechanism moved; each class carries its own rate."""
    entries = [
        {"id": "p-1", "class": "paraphrase", "q": "a", "expect": ["good"], "note": "n"},
        {"id": "p-2", "class": "paraphrase", "q": "b", "expect": ["nope"], "note": "n"},
        {"id": "x-1", "class": "exact-token", "q": "c", "expect": ["good"], "note": "n"},
        {"id": "n-1", "class": "negative", "q": "d", "expect": [], "note": "n"},
    ]
    res = E.run(entries, query_fn=lambda q, tb: [_node("good")] if q in ("a", "c") else [])
    assert res["by_class"]["paraphrase"]["recall5_rate"] == 0.5
    assert res["by_class"]["exact-token"]["recall5_rate"] == 1.0
    assert res["by_class"]["negative"]["pass_rate"] == 1.0
    assert res["positive"]["recall5_rate"] == round(2 / 3, 3)
    assert res["by_class"]["paraphrase"]["failing"] == ["p-2"]


def test_query_exception_is_a_failure_not_a_skip() -> None:
    entries = [{"id": "p-1", "class": "paraphrase", "q": "a", "expect": ["g"], "note": "n"}]

    def boom(_q, _tb):
        raise RuntimeError("db gone")

    res = E.run(entries, query_fn=boom)
    assert res["by_class"]["paraphrase"]["passed"] == 0
    assert "RuntimeError" in res["detail"][0]["reason"]


# --------------------------------------------------------------- D. the verdict is a gate

def _result(recall: float, contamination_pass: int = 2, guard: float = 1.0) -> dict:
    return {
        "generated": "now", "questions": 10,
        "positive": {"n": 10, "recall5_rate": recall, "top1_rate": 0.1, "pass_rate": recall},
        "guards": {"n": 4, "pass_rate": guard},
        "by_class": {
            "paraphrase": {"n": 4, "passed": 2, "pass_rate": 0.5, "recall5_rate": recall,
                           "top1_rate": 0.0, "failing": []},
            "contamination": {"n": 2, "passed": contamination_pass, "pass_rate": 1.0,
                              "failing": [] if contamination_pass == 2 else ["con-01"]},
        },
        "detail": [],
    }


def test_verdict_gates() -> None:
    base = _result(0.50)
    baseline = {"positive": base["positive"], "guards": base["guards"],
                "by_class": base["by_class"]}

    assert E.verdict(_result(0.50), baseline)[0] == E.PASS

    # Contamination is absolute — a green retrieval number does not rescue it.
    status, reasons = E.verdict(_result(0.90, contamination_pass=1), baseline)
    assert status == E.FAIL and any("CONTAMINATION" in r for r in reasons)

    # A real drop fails.
    assert E.verdict(_result(0.30), baseline)[0] == E.FAIL

    # Noise inside tolerance does not.
    assert E.verdict(_result(0.47), baseline)[0] == E.PASS

    # A jump warns rather than passing silently — sudden improvement is usually a
    # question set that drifted, and it must be confirmed before being re-pinned.
    status, reasons = E.verdict(_result(0.90), baseline)
    assert status == E.WARN and any("ROSE" in r for r in reasons)

    # Guards slipping (the Brain starts answering unanswerable questions) warns.
    assert E.verdict(_result(0.50, guard=0.5), baseline)[0] == E.WARN

    # No baseline is not a pass.
    assert E.verdict(_result(0.50), None)[0] == E.WARN


def test_class_floor_breach_fails_even_without_a_baseline_drop() -> None:
    """Per-class ABSOLUTE floors (Leg 1 expansion, 2026-08-06). A relative-tolerance check
    only fires on a DROP from the previous run, so a class that is pinned below its floor
    on BOTH the baseline and the current run — i.e. never regressed, was simply always
    below the line — would sail through every check above this one as a silent PASS. The
    floor exists to catch that case; this proves it does, and proves a floor-conforming
    class does NOT trip it."""
    floors = {"paraphrase": 0.40}
    base = _result(0.50)
    base["by_class"]["paraphrase"]["recall5_rate"] = 0.50
    baseline = {"positive": base["positive"], "guards": base["guards"],
                "by_class": base["by_class"], "_floors": floors}

    # Same class rate on both sides — no drop, so the relative tolerance checks above
    # would all read this as unchanged. The floor must fire anyway.
    breached = _result(0.50)
    breached["by_class"]["paraphrase"]["recall5_rate"] = 0.30  # below the 0.40 floor
    status, reasons = E.verdict(breached, baseline)
    assert status == E.FAIL, "a class below its pinned floor must FAIL regardless of drift"
    assert any("CLASS FLOOR BREACH" in r and "paraphrase" in r for r in reasons)

    # A class sitting AT or above its floor must not trip this specific check (other
    # checks in the gate may still fire independently — isolate to the floor reason).
    conforming = _result(0.50)
    conforming["by_class"]["paraphrase"]["recall5_rate"] = 0.45  # above the 0.40 floor
    _, reasons2 = E.verdict(conforming, baseline)
    assert not any("CLASS FLOOR BREACH" in r for r in reasons2)

    # `load_class_floors` prefers the baseline's pinned `_floors` over the code constant —
    # a re-pin can raise a floor with evidence and no code change.
    assert E.load_class_floors(baseline) == floors
    assert E.load_class_floors(None) == E.PER_CLASS_FLOORS
    assert E.load_class_floors({"_floors": {}}) == E.PER_CLASS_FLOORS, \
        "an empty _floors block must fall back, not disarm the gate"


def test_expected_ids_resolve_against_live_index() -> None:
    """The enforcement the fixtures README promises. A renamed document churns its node ID
    (IDs are slug+hash of title/path); the entry can then never pass and quietly drags its
    class rate down — indistinguishable from a ranker regression at the alert."""
    stale = E.check_expected_ids(E.load_golden())
    assert not stale, (
        "golden entries reference node IDs that are no longer in the live index — repair the "
        f"golden set, do not lower the baseline: {stale}")


def test_check_expected_ids_reports_only_the_missing() -> None:
    real = E.load_golden()[0]["expect"][0]
    entries = [
        {"id": "a-01", "class": "paraphrase", "q": "q", "expect": [real], "note": "n"},
        {"id": "a-02", "class": "paraphrase", "q": "q",
         "expect": ["mem_definitely_not_a_real_node_000000"],
         "alternates": [real], "note": "n"},
        {"id": "a-03", "class": "recency", "q": "q", "expect": [real],
         "unexpected": ["mem_also_not_real_111111"], "note": "n"},
    ]
    stale = E.check_expected_ids(entries)
    assert "a-01" not in stale, "flagged an ID that resolves"
    assert stale["a-02"] == ["mem_definitely_not_a_real_node_000000"]
    # `unexpected` IDs matter too: a stale one silently stops guarding against the
    # superseded answer it names, and the entry then passes for the wrong reason.
    assert stale["a-03"] == ["mem_also_not_real_111111"]


def test_stale_fixtures_downgrade_a_regression_to_warn() -> None:
    """With 19 positive entries one question is worth 0.053, so a tight tolerance plus one
    renamed document = a COVENANT page about a ranker that never changed. "Someone renamed a
    document" and "the ranker got worse" have opposite remedies and must not arrive alike."""
    base = _result(0.50)
    baseline = {"positive": base["positive"], "guards": base["guards"],
                "by_class": base["by_class"]}

    assert E.verdict(_result(0.20), baseline)[0] == E.FAIL, "a real regression must still FAIL"

    stale = _result(0.20)
    stale["unresolved"] = {"para-01": ["mem_renamed_doc_abc123"]}
    status, reasons = E.verdict(stale, baseline)
    assert status == E.WARN, "a regression with stale fixtures must not FAIL"
    assert any("STALE FIXTURE" in r for r in reasons)
    assert any("mem_renamed_doc_abc123" in r for r in reasons), "the stale ID must be named"
    assert any("NOT attributed to the ranker" in r for r in reasons)

    # Contamination stays absolute — a stale fixture buys no amnesty for the scorecard
    # grading itself again.
    poisoned = _result(0.20, contamination_pass=1)
    poisoned["unresolved"] = {"para-01": ["mem_renamed_doc_abc123"]}
    assert E.verdict(poisoned, baseline)[0] == E.FAIL


def test_tolerance_survives_one_question_of_noise() -> None:
    """One question of 19 is 0.053: the gate must not fire on it, and must fire on two."""
    n = 19
    baseline = {"positive": {"n": n, "recall5_rate": round(8 / n, 3)},
                "guards": {"pass_rate": 1.0}, "by_class": {}}
    one_down = _result(round(7 / n, 3))
    one_down["by_class"] = {}
    assert E.verdict(one_down, baseline)[0] == E.PASS, "fired on one question of noise"
    two_down = _result(round(6 / n, 3))
    two_down["by_class"] = {}
    assert E.verdict(two_down, baseline)[0] == E.FAIL, "failed to fire on a two-question drop"


def test_forbid_fixture_is_honoured_outside_the_contamination_class() -> None:
    """The field was documented and never read. A fixture surfacing in a paraphrase question
    is the same defect — and must fail even when the expected node is also present."""
    e = {"id": "p-1", "class": "paraphrase", "q": "?", "expect": ["good"],
         "forbid_fixture": True, "note": "n"}
    assert E.score_entry(e, [_node("good")])["passed"]
    r = E.score_entry(e, [_node("good"), _node("mem_x", "ARCTURION_Brain/engine/eval.py")])
    assert not r["passed"], "fixture hit ignored because the expected node was also present"
    assert r["fixture_hits"] == ["mem_x"]
    # Without the flag the same pack scores on relevance alone.
    assert E.score_entry({k: v for k, v in e.items() if k != "forbid_fixture"},
                         [_node("good"), _node("mem_x", "ARCTURION_Brain/engine/eval.py")]
                         )["passed"]


def test_crash_alerts_covenant_and_exits_two() -> None:
    """A gate that can die quietly is not a gate: without this a crash writes a traceback to
    an unread .err, `_emit_inbox_alert` never runs, and from the inbox's point of view
    'didn't run' looks exactly like 'passed'."""
    tmp = Path(tempfile.mkdtemp(prefix="crash_test_"))
    orig_inbox, orig_run = C.COVENANT_INBOX, E.run
    try:
        C.COVENANT_INBOX = tmp / "inbox.md"

        def boom(*_a, **_k):
            raise RuntimeError("golden set unreadable")

        E.run = boom
        code = E.main(["--token-budget", "100"])
        assert code == 2, f"crash must exit 2 (distinct from 1=FAIL), got {code}"
        body = C.COVENANT_INBOX.read_text()
        assert "CRASHED" in body and "RuntimeError" in body, body
        assert "Eval FAIL" not in body, "a crash must not be reported as a FAIL verdict"
        assert "must not be quoted as current" in body, "must say the last report is stale"

        # --no-alert still suppresses, and still exits 2.
        C.COVENANT_INBOX = tmp / "inbox2.md"
        assert E.main(["--no-alert"]) == 2
        assert not C.COVENANT_INBOX.exists()
    finally:
        C.COVENANT_INBOX, E.run = orig_inbox, orig_run
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_unwritable_inbox_reports_to_stderr_instead_of_vanishing() -> None:
    """The alerting path had a bare `except Exception: pass`. A module whose entire purpose
    is "never fail quietly" cannot have a mute failure path of its own — if the inbox is
    unwritable, THAT is the alert, and it has to reach the LaunchAgent's .err file."""
    import contextlib
    import io
    tmp = Path(tempfile.mkdtemp(prefix="inbox_ro_"))
    orig = C.COVENANT_INBOX
    try:
        # A path whose parent is a FILE — mkdir and open both fail with OSError.
        blocker = tmp / "blocker"
        blocker.write_text("not a directory")
        C.COVENANT_INBOX = blocker / "inbox.md"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            E._append_inbox("### heading", ["- body line"])   # must not raise
        out = err.getvalue()
        assert "UNWRITABLE" in out, f"inbox failure was silent: {out!r}"
        assert "NOT delivered" in out
        # The undelivered content is echoed so it stays recoverable from the log.
        assert "### heading" in out and "- body line" in out
    finally:
        C.COVENANT_INBOX = orig
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_corrupt_baseline_is_distinguishable_from_an_absent_one() -> None:
    """Both return None and both leave the gate unable to detect a regression — but a
    truncated baseline.json silently downgrading to "no pinned baseline" is the one state in
    which nothing can regress, and it must not look like an ordinary first run."""
    import contextlib
    import io
    tmp = Path(tempfile.mkdtemp(prefix="baseline_"))
    try:
        assert E.load_baseline(tmp / "absent.json") is None

        corrupt = tmp / "corrupt.json"
        corrupt.write_text('{"positive": {"recall5_rate": 0.3')   # truncated
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert E.load_baseline(corrupt) is None
        out = err.getvalue()
        assert "not valid JSON" in out and "NOT being applied" in out, out

        wrong_type = tmp / "list.json"
        wrong_type.write_text("[1, 2, 3]")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            assert E.load_baseline(wrong_type) is None
        assert "expected an object" in err.getvalue()

        good = tmp / "good.json"
        good.write_text('{"positive": {"recall5_rate": 0.3}}')
        assert E.load_baseline(good) == {"positive": {"recall5_rate": 0.3}}
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_watchdog_ring_watches_the_nightly_gate() -> None:
    """An unwatched scheduled job is the dead-notifier class this programme closes. If the
    LaunchAgent is ever dropped, the ring must notice."""
    manifest = (Path.home() / ".claude" / "skills" / "universe-maintenance"
                / "required_services.json")
    if not manifest.exists():
        print("     (skipped: universe-maintenance manifest not on this host)")
        return
    spec = json.loads(manifest.read_text())["services"].get(
        "com.arcturion.brain-retrieval-eval")
    assert spec, "the nightly retrieval gate is not in the watchdog ring"
    assert spec["resident"] is False, "it is a periodic job, not a resident daemon"
    # Its exit code is a VERDICT (1=FAIL, 2=crash), not a liveness signal. Without this flag
    # a genuine FAIL night reads as a dead service and triggers a restart loop.
    assert spec.get("exit_codes_are_status") is True, \
        "a FAIL verdict would be mistaken for a dead service"


def test_inbox_alert_fires_only_on_fail() -> None:
    """The alert is the whole reason this is a gate rather than a report. Exercised against
    a temp inbox — never the real COVENANT one."""
    tmp = Path(tempfile.mkdtemp(prefix="inbox_test_"))
    orig = C.COVENANT_INBOX
    try:
        C.COVENANT_INBOX = tmp / "inbox.md"
        res = _result(0.10, contamination_pass=1)

        E._emit_inbox_alert(E.PASS, res, ["ignored"])
        assert not C.COVENANT_INBOX.exists(), "alerted on a PASS"
        E._emit_inbox_alert(E.WARN, res, ["ignored"])
        assert not C.COVENANT_INBOX.exists(), "alerted on a WARN"

        E._emit_inbox_alert(E.FAIL, res, ["CONTAMINATION: fixture is retrievable again"])
        body = C.COVENANT_INBOX.read_text()
        assert "Retrieval Eval FAIL" in body and "CONTAMINATION" in body, body
        assert "index/retrieval_eval.md" in body, "alert must name its source"
    finally:
        C.COVENANT_INBOX = orig
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_report_never_contains_question_text() -> None:
    """Golden question text in a written report is how the original contamination spread.
    The report names entries by id only."""
    tmp = Path(tempfile.mkdtemp(prefix="report_test_"))
    try:
        entries = E.load_golden()
        res = E.run(entries[:3], query_fn=lambda q, tb: [])
        out = E.write_report(res, E.WARN, ["x"], tmp / "r.md").read_text()
        for e in entries[:3]:
            assert e["q"] not in out, f"report leaked question text for {e['id']}"
            assert e["id"] in out, f"report omitted entry {e['id']}"
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def run() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  ✅ {t.__name__}")
        except AssertionError as e:
            failures += 1
            print(f"  ❌ {t.__name__}: {e}")
    print(f"\n{'PASS' if not failures else 'FAIL'} — retrieval eval: "
          f"{len(tests) - failures}/{len(tests)} tests")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run())
