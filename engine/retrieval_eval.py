"""engine.retrieval_eval — the Brain's retrieval ruler (Leg 1, retrieval-honesty 2026-08-04).

Replaces `engine/eval.py`, which was deleted rather than kept alongside this module. Two
reasons it could not stay:

  * **It could not fail.** Its golden questions lived in its own source, the walker indexed
    that source, and it therefore matched every question it was being graded on. Measured
    live 2026-08-04 it scored 6/6 with `top1_canonical_rate = 1.0` while four of six top-1
    answers *were eval.py*. `config.py` justified the BFS expansion caps with "verified
    against engine.eval … to not regress answer quality" — caps tuned against a scorer that
    could not detect a regression.
  * **Two scorecards is worse than one.** A green number from the old scorer sitting beside
    a red number from this one is an invitation to quote whichever is convenient. There is
    one ruler.

WHAT CHANGED
------------
1. **Questions live outside every walk root.** `engine/tests/fixtures/golden.jsonl` is
   pruned by `config.BRAIN_FIXTURE_RELS` and swept by `maintenance.skiplist_gc`.
2. **Scoring is on node IDs, never substrings.** The old `_on_topic` asked "does the wanted
   word appear in the top-5 title+summary" — which any document mentioning the topic passes,
   including the eval. `expect` holds node IDs; a wrong document with the right word in it
   now fails.
3. **Per class, not one number.** paraphrase / common-token / exact-token / recency /
   negative / contamination are separate failure mechanisms with separate remedies. One
   blended rate hides which one moved — and later legs of this programme each target one
   class, so each needs its own denominator.
4. **Negative controls count.** "Always answers" is a defect. Five questions have no answer
   in the corpus and the pack must come back empty.
5. **It is scheduled.** `com.arcturion.brain-retrieval-eval` runs it nightly after the 02:30
   reindex and alerts the COVENANT inbox on FAIL. An unscheduled eval is not a gate.

USAGE
    python3 -m engine.retrieval_eval                  # score, write report, exit 1 on FAIL
    python3 -m engine.retrieval_eval --json           # machine-readable
    python3 -m engine.retrieval_eval --write-baseline # re-pin the baseline (deliberate act)
    python3 -m engine.retrieval_eval --no-alert       # never touch the COVENANT inbox
"""
from __future__ import annotations

import argparse
import math
import json
import os
import subprocess
import sys
from pathlib import Path

from . import config as C
from .resolver import is_archive_path
from .util import now_iso

GOLDEN_PATH = Path(__file__).parent / "tests" / "fixtures" / "golden.jsonl"
REPRESENTATIVE_QUALITY_PATH = (Path(__file__).parent / "tests" / "fixtures" /
                               "representative_quality.jsonl")
HELDOUT_PATH = Path(__file__).parent / "tests" / "fixtures" / "heldout.jsonl"
BASELINE_PATH = Path(__file__).parent / "tests" / "fixtures" / "baseline.json"
REPORT_PATH = C.GRAPH_DIR / "retrieval_eval.md"
# Machine-readable twin of the report, for `/metrics` (Leg 5). A JSON sidecar rather than
# re-scoring on demand: a scoring run asks 26 live queries, and a monitoring endpoint that
# costs 26 queries per hit is a load generator wearing a dashboard. Ids and rates only —
# never question TEXT, same rule as `write_report`.
SCORE_PATH = C.GRAPH_DIR / "retrieval_score.json"

# Classes whose entries assert a positive answer (top1 / recall@5 are meaningful).
POSITIVE_CLASSES = ("paraphrase", "common-token", "exact-token", "recency")
# Classes that assert an ABSENCE. They have no recall; they pass or they don't.
GUARD_CLASSES = ("negative", "contamination")
VALID_CLASSES = POSITIVE_CLASSES + GUARD_CLASSES

RECALL_DEPTH = 5

# Regression tolerances, applied against fixtures/baseline.json.
#
# Overall is tighter than per-class on purpose: a single class of 4-6 questions is noisy
# (one entry is 17-25 points), while the pooled positive rate over ~19 questions moves only
# on a real change. Both are floors relative to the pinned baseline, never absolute targets —
# an absolute target invites tuning the question set until it is met.
#
# 0.08, not 0.05: with 19 positive entries one question is worth 0.053, so a 0.05 tolerance
# made a SINGLE question flipping trip FAIL and page the COVENANT inbox. 0.08 still fires on
# two questions — tight enough to be a gate, loose enough not to cry wolf on one. The other
# half of that fix is `check_expected_ids()`: the likeliest cause of one question flipping is
# a renamed document leaving a stale expected node ID, which is a fixture problem, not a
# ranker regression, and must never be reported as one.
OVERALL_TOLERANCE = 0.08
PER_CLASS_TOLERANCE = 0.20

# Per-class ABSOLUTE floors (Leg 1 expansion, 2026-08-06). Distinct from the relative
# tolerances above: PER_CLASS_TOLERANCE only fires when a class DROPS vs its own baseline
# run, so a class that was already weak (e.g. common-token at 0.2) can stay weak forever
# without ever tripping a gate. A floor is a line a class must never be BELOW, tuning or
# no tuning, corpus growth or no corpus growth — it catches "always was bad, still is"
# as well as "used to be fine, now isn't." Pinned beside `baseline.json` (embedded in its
# `_floors` key so the two travel together) and duplicated here as the code-level source
# of truth; `load_class_floors()` prefers the baseline file when present so a re-pin can
# raise floors with evidence without a code change, but never silently loses them if the
# baseline is corrupt. Values are `recall5_rate` for positive classes and `pass_rate` for
# guard classes. Set from the 2026-08-06 post-expansion measurement, each with headroom
# below the measured value — see docs/retrieval-baseline-2026-08-04.md's re-baseline note
# for the numbers these were derived from.
PER_CLASS_FLOORS: dict[str, float] = {
    # Measured 2026-08-06 on the 57-question golden set (n=11 per positive class, n=11
    # negative, n=2 contamination): paraphrase 0.273, common-token 0.182, exact-token
    # 0.909, recency 0.545, negative pass_rate 0.364, contamination 1.0. Each floor below
    # sits with headroom under that number — enough that normal 1-2-question noise (worth
    # ~0.09 per question at n=11) does not trip it, tight enough that a real collapse
    # (e.g. exact-token falling toward common-token's territory) still FAILs outright
    # rather than waiting for a relative-tolerance comparison against a baseline run that
    # might itself have been mid-collapse.
    "paraphrase": 0.15,
    "common-token": 0.05,
    "exact-token": 0.50,
    "recency": 0.25,
    "negative": 0.20,
    "contamination": 1.00,
}

# Useful-answer objectives are intentionally separate from the regression ruler above.
# The regression verdict answers "did retrieval get worse?"; this policy answers "is the
# service useful enough to rely on?".  Keeping the targets out of baseline.json prevents a
# re-pin from laundering weak-but-stable retrieval into a quality PASS.
QUALITY_TARGETS: dict[str, float] = {
    "positive.recall5_rate": 0.75,
    "positive.top1_rate": 0.50,
    "guards.pass_rate": 0.90,
    "by_class.paraphrase.recall5_rate": 0.55,
    "by_class.common-token.recall5_rate": 0.45,
    "by_class.exact-token.recall5_rate": 0.90,
    "by_class.recency.recall5_rate": 0.70,
    "by_class.negative.pass_rate": 0.80,
    "by_class.contamination.pass_rate": 1.00,
}

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_ICON = {PASS: "✅", WARN: "⚠️", FAIL: "🛑"}

# Statuses that mean "this is a live, trustworthy node" (carried over from engine/eval.py —
# the canonical check itself was never the broken part).
_NONCANONICAL_STATUS = {"superseded", "archived", "deprecated", "draft", "retired"}


# --------------------------------------------------------------------------- helpers

def is_canonical(node: dict | None) -> bool:
    """A node is canonical if its status is not a stale/superseded marker and its path is
    not an archive/backup/staging copy."""
    if not node:
        return False
    if (node.get("status") or "").lower() in _NONCANONICAL_STATUS:
        return False
    return not is_archive_path(node.get("path"))


# Additional artifact IDs can be supplied by the operator for this corpus.
EVAL_ARTIFACT_NODE_IDS = frozenset(
    item.strip() for item in os.environ.get("ARCTURION_BRAIN_EVAL_ARTIFACT_IDS", "").split(",")
    if item.strip()
)


def is_fixture_node(node: dict | None) -> bool:
    """True if a returned node is one of the Brain's own eval fixtures — i.e. the corpus is
    contaminated again. Checks the path (authoritative); belt-and-braces, the known id of
    the 2026-08-04 offender in case a node survives with a rewritten path; and the explicit
    EVAL_ARTIFACT_NODE_IDS list of write-ups about this eval that quote the fixtures."""
    if not node:
        return False
    if C.is_brain_fixture_path(node.get("path") or ""):
        return True
    node_id = node.get("id") or ""
    if node_id in EVAL_ARTIFACT_NODE_IDS:
        return True
    return node_id.startswith("mem_eval_py_")


def load_golden(path: Path | None = None) -> list[dict]:
    """Read and validate the golden set. Raises on a malformed entry rather than silently
    scoring a smaller set — a question that quietly stops being asked is a hole in the ruler."""
    path = Path(path or GOLDEN_PATH)
    entries: list[dict] = []
    seen_ids: set[str] = set()
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        try:
            e = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: not valid JSON — {exc}") from exc
        for field in ("id", "class", "q", "note"):
            if not e.get(field):
                raise ValueError(f"{path}:{lineno}: missing required field {field!r}")
        if e["class"] not in VALID_CLASSES:
            raise ValueError(f"{path}:{lineno}: unknown class {e['class']!r}")
        if e["id"] in seen_ids:
            raise ValueError(f"{path}:{lineno}: duplicate id {e['id']!r}")
        if e["class"] in POSITIVE_CLASSES and not e.get("expect"):
            raise ValueError(f"{path}:{lineno}: {e['id']} is a {e['class']} entry with no "
                             f"expected node IDs — it can never pass or fail meaningfully")
        seen_ids.add(e["id"])
        entries.append(e)
    if not entries:
        raise ValueError(f"{path}: golden set is empty")
    return entries


# The nightly reindex's advisory lock. SHARED with brain-ingest.sh (the ~20 s live
# watcher), so its mere presence does NOT mean a reindex — the holder's command line is
# what distinguishes the two.
REINDEX_LOCK = C.WORK_ROOT / ".arcturion" / "brain-write.lock"
REINDEX_JOB = "com.arcturion.brain-walker"


def reindex_in_progress() -> tuple[bool, str]:
    """Is the nightly walk still running? Returns (in_progress, human reason).

    The eval fires at 03:30 against a reindex that starts at 02:30 — a fixed offset with no
    completion signal. An overrunning walk (the corpus grows; embedding 76k nodes is not a
    fixed cost) means the gate scores a HALF-REBUILT graph and publishes that as the nightly
    verdict. Recall would crater, the gate would FAIL, and it would page COVENANT about a
    ranker that never changed — the same false-alert class as a stale fixture ID, and the
    same remedy: refuse to score rather than score something meaningless.

    Guarded in code rather than by chaining the plists, because a plist chain only covers the
    scheduled path. A Founder or agent running `engine.reindex` by hand at any hour hits the
    identical hazard, and this catches that too.

    Two independent signals, either sufficient, both fail-open (a detector that crashes must
    not take the gate down with it — its worst case is a missed skip, not a missed run):

      1. The advisory lock's PID is alive AND its command line is a reindex. The PID check
         matters because the lock is not removed if a reindex is killed; the command check
         matters because brain-ingest.sh holds the same lock every ~20 s, and treating that
         as "reindex in progress" would skip the eval essentially every night.
      2. launchd reports the nightly job as running.
    """
    # 1 — lock file, holder verified
    try:
        if REINDEX_LOCK.exists():
            raw = REINDEX_LOCK.read_text(encoding="utf-8", errors="replace").strip()
            pid = int(raw.split()[0]) if raw.split() else 0
            if pid > 0:
                alive = True
                try:
                    os.kill(pid, 0)          # signal 0 = liveness probe only
                except ProcessLookupError:
                    alive = False
                except PermissionError:
                    alive = True             # exists, owned by someone else
                if alive:
                    cmd = subprocess.run(["/bin/ps", "-o", "command=", "-p", str(pid)],
                                         capture_output=True, text=True, timeout=10).stdout
                    if "reindex" in cmd:
                        return True, (f"{REINDEX_LOCK.name} held by live pid {pid} "
                                      f"running a reindex")
    except (OSError, ValueError, subprocess.SubprocessError):
        pass  # fail open — see docstring

    # 2 — launchd's own view of the nightly job
    try:
        out = subprocess.run(["/bin/launchctl", "print", f"gui/{os.getuid()}/{REINDEX_JOB}"],
                             capture_output=True, text=True, timeout=15).stdout
        for line in out.splitlines():
            if line.strip() == "state = running":
                return True, f"launchd reports {REINDEX_JOB} still running"
    except (OSError, subprocess.SubprocessError):
        pass  # fail open

    return False, ""


def check_expected_ids(entries: list[dict], db_path=None) -> dict[str, list[str]]:
    """Resolve every referenced node ID against the live index. Returns {entry_id: [missing]}.

    Node IDs are slug+hash of title/path, so **renaming a canonical document churns its ID**.
    A stale expected ID makes its question permanently unanswerable, which looks exactly like
    a ranker regression: the class rate drops and the gate fires. The two have opposite
    remedies — one is "fix the fixture", the other is "fix retrieval" — so they must be
    distinguishable before the alert is written, not after someone investigates.

    Read-only, and failure-tolerant: if the DB cannot be opened this returns {} rather than
    raising, because an unavailable index is the query path's problem to report, not this
    check's."""
    import sqlite3
    from . import db as _DB

    referenced: set[str] = set()
    for e in entries:
        for key in ("expect", "alternates", "unexpected"):
            referenced.update(e.get(key) or [])
    if not referenced:
        return {}
    try:
        conn = _DB.connect(db_path or C.BRAIN_DB)
    except sqlite3.Error:
        return {}
    try:
        present: set[str] = set()
        ids = sorted(referenced)
        for i in range(0, len(ids), 400):  # chunked: SQLite caps host parameters
            chunk = ids[i:i + 400]
            q = f"SELECT id FROM nodes WHERE id IN ({','.join('?' * len(chunk))})"
            present.update(r[0] for r in conn.execute(q, chunk))
    except sqlite3.Error:
        return {}
    finally:
        conn.close()

    stale: dict[str, list[str]] = {}
    for e in entries:
        missing = sorted({n for key in ("expect", "alternates", "unexpected")
                          for n in (e.get(key) or []) if n not in present})
        if missing:
            stale[e["id"]] = missing
    return stale


def _default_query(topic: str, token_budget: int) -> list[dict]:
    from . import brain  # late import: brain imports this module's sibling resolver
    return brain.query(topic, token_budget=token_budget).nodes


# --------------------------------------------------------------------------- scoring

def score_entry(entry: dict, nodes: list[dict]) -> dict:
    """Score one golden entry against the nodes its question returned."""
    cls = entry["class"]
    top = nodes[0] if nodes else None
    top_id = (top or {}).get("id")
    head = nodes[:RECALL_DEPTH]
    head_ids = [n.get("id") for n in head]

    res = {
        "id": entry["id"], "class": cls, "top1_id": top_id,
        "nodes_returned": len(nodes), "reason": "",
    }

    # Optional lifecycle assertions inspect the entire pack, beyond top-k recall.
    by_id = {n.get("id"): n for n in nodes}
    forbidden = sorted(set(entry.get("forbid_anywhere", [])) & set(by_id))
    version_errors = [nid for nid, digest in entry.get("version_expectations", {}).items()
                      if by_id.get(nid, {}).get("content_hash") != digest]
    if forbidden or version_errors:
        res.update(passed=False, lifecycle_errors={"forbidden": forbidden,
                                                  "version_mismatch": version_errors},
                   reason="lifecycle evidence contract failed")
        return res

    if cls == "negative":
        limit = int(entry.get("max_nodes", 0))
        res["passed"] = len(nodes) <= limit
        res["reason"] = ("" if res["passed"] else
                         f"answered a question with no answer — {len(nodes)} nodes, "
                         f"top1={top_id}")
        return res

    # `forbid_fixture` is implicit for the contamination class and available to ANY entry —
    # a fixture surfacing in a paraphrase question is the same defect, and an entry that
    # opted into the check should get it. Checked before the positive scoring below so a
    # fixture hit fails the entry even when the expected node is also present.
    if cls == "contamination" or entry.get("forbid_fixture"):
        hits = [n.get("id") for n in nodes if is_fixture_node(n)]
        res["fixture_hits"] = hits
        if hits:
            res["passed"] = False
            res["reason"] = f"eval fixture is retrievable again: {hits}"
            return res
        if cls == "contamination":
            res["passed"] = True
            return res

    expect = set(entry.get("expect") or [])
    alternates = set(entry.get("alternates") or [])
    unexpected = set(entry.get("unexpected") or [])
    labeled = expect | alternates

    res["top1"] = top_id in expect
    res["recall5"] = bool(labeled & set(head_ids))
    poisoned = sorted(unexpected & set(head_ids))
    res["superseded_hits"] = poisoned
    res["declares_unexpected"] = bool(unexpected)
    res["top1_canonical"] = is_canonical(top)

    # precision@5 — literally (expected+alternates ∩ top5) / 5, NOT the classic
    # relevant-retrieved/k. `labeled` is usually 1-3 ids against a 5-slot window, so this
    # reads low by construction; it is an instrument for "how much of the returned window
    # is a hit", never a target to push toward 1.0. Denominator is always RECALL_DEPTH,
    # not len(head), so a short pack (fewer than 5 nodes returned) is not rewarded for
    # having fewer slots to be wrong in.
    hit_count = len([h for h in head_ids if h in labeled])
    res["precision5"] = round(hit_count / RECALL_DEPTH, 3)

    # MRR — reciprocal rank of the first labeled hit anywhere in the FULL returned list
    # (not capped to RECALL_DEPTH), 0 if no labeled node is returned at all. Unlike
    # recall@5/top1, MRR is sensitive to a hit sliding from rank 1 to rank 4 even though
    # both still "pass" recall@5 — the ranking-quality signal recall@5 cannot see.
    rr = 0.0
    for i, nid in enumerate(nodes, start=1):
        if nid.get("id") in labeled:
            rr = 1.0 / i
            break
    res["reciprocal_rank"] = round(rr, 4)

    # wrong-context — of the (up to 5) nodes actually returned, how many carry neither an
    # expected nor an alternate label. A pack can pass recall@5 (the right node is
    # SOMEWHERE in the top 5) while still being mostly noise around it; this is the count
    # that number hides. Denominator is len(head) (nodes actually returned, capped at 5),
    # not RECALL_DEPTH, because a pack that returns 2 nodes has only 2 chances to be wrong.
    wrong = [h for h in head_ids if h not in labeled]
    res["wrong_context_hits"] = len(wrong)
    res["wrong_context_slots"] = len(head_ids)

    res["passed"] = res["recall5"] and not poisoned
    if poisoned:
        res["reason"] = f"superseded answer in top {RECALL_DEPTH}: {poisoned}"
    elif not res["recall5"]:
        res["reason"] = f"no expected node in top {RECALL_DEPTH} (top1={top_id})"
    return res


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 3) if den else None


def run(entries: list[dict] | None = None, token_budget: int = 1500,
        query_fn=None, verbose: bool = False) -> dict:
    """Run every golden question and aggregate per class.

    Before scoring, every referenced node ID is resolved against the live index so a stale
    fixture cannot be mistaken for a ranker regression (see `check_expected_ids`). The check
    is skipped when a caller supplies its own `query_fn`, because that caller is scoring
    against its own graph and its IDs are not expected to exist in the live one."""
    entries = entries if entries is not None else load_golden()
    live = query_fn is None
    query_fn = query_fn or _default_query
    unresolved = check_expected_ids(entries) if live else {}
    if verbose and unresolved:
        for eid, missing in unresolved.items():
            print(f"  {_ICON[WARN]} {eid:<8} [fixture] expected node ID no longer in the "
                  f"index: {', '.join(missing)}")

    details = []
    for e in entries:
        try:
            nodes = query_fn(e["q"], token_budget)
        except Exception as exc:  # a query that explodes is a failure, not a skip
            details.append({"id": e["id"], "class": e["class"], "passed": False,
                            "nodes_returned": 0, "top1_id": None,
                            "reason": f"query raised {type(exc).__name__}: {exc}"})
            continue
        r = score_entry(e, nodes)
        details.append(r)
        if verbose:
            print(f"  {_ICON[PASS] if r['passed'] else _ICON[FAIL]} {r['id']:<8} "
                  f"[{r['class']}] {r['reason'] or 'ok'}")

    by_class: dict[str, dict] = {}
    for cls in VALID_CLASSES:
        rows = [d for d in details if d["class"] == cls]
        if not rows:
            continue
        blk = {"n": len(rows), "passed": sum(1 for d in rows if d["passed"])}
        blk["pass_rate"] = _rate(blk["passed"], blk["n"])
        if cls in POSITIVE_CLASSES:
            blk["top1"] = sum(1 for d in rows if d.get("top1"))
            blk["top1_rate"] = _rate(blk["top1"], blk["n"])
            blk["recall5"] = sum(1 for d in rows if d.get("recall5"))
            blk["recall5_rate"] = _rate(blk["recall5"], blk["n"])
            blk["precision5_rate"] = round(
                sum(d.get("precision5") or 0 for d in rows) / blk["n"], 3
            ) if rows else None
            blk["mrr"] = round(sum(d.get("reciprocal_rank") or 0 for d in rows) / blk["n"], 3)
            declaring = [d for d in rows if d.get("declares_unexpected")]
            blk["superseded_in_top5_rate"] = _rate(
                sum(1 for d in declaring if d.get("superseded_hits")), len(declaring)
            )
            wrong_sum = sum(d.get("wrong_context_hits") or 0 for d in rows)
            slot_sum = sum(d.get("wrong_context_slots") or 0 for d in rows)
            blk["wrong_context_rate"] = _rate(wrong_sum, slot_sum)
        blk["failing"] = [d["id"] for d in rows if not d["passed"]]
        by_class[cls] = blk

    pos = [d for d in details if d["class"] in POSITIVE_CLASSES]
    guards = [d for d in details if d["class"] in GUARD_CLASSES]

    # Pooled versions of the four new metrics, over ALL positive entries — the same
    # "one blended number plus per-class" pattern as recall@5/top1 above, so a reader who
    # only has 30 seconds sees one line and a reader auditing a regression sees which
    # class moved.
    precision5_num = sum(d.get("precision5") or 0 for d in pos)
    mrr_avg = round(sum(d.get("reciprocal_rank") or 0 for d in pos) / len(pos), 3) if pos else None
    declaring_all = [d for d in pos if d.get("declares_unexpected")]
    superseded_rate = _rate(sum(1 for d in declaring_all if d.get("superseded_hits")), len(declaring_all))
    wrong_num = sum(d.get("wrong_context_hits") or 0 for d in pos)
    wrong_den = sum(d.get("wrong_context_slots") or 0 for d in pos)

    return {
        "generated": now_iso(),
        "questions": len(details),
        "unresolved": unresolved,
        "positive": {
            "n": len(pos),
            "recall5_rate": _rate(sum(1 for d in pos if d.get("recall5")), len(pos)),
            "top1_rate": _rate(sum(1 for d in pos if d.get("top1")), len(pos)),
            "pass_rate": _rate(sum(1 for d in pos if d["passed"]), len(pos)),
            "precision5_rate": round(precision5_num / len(pos), 3) if pos else None,
            "mrr": mrr_avg,
            "superseded_in_top5_rate": superseded_rate,
            "wrong_context_rate": _rate(wrong_num, wrong_den),
        },
        "guards": {
            "n": len(guards),
            "pass_rate": _rate(sum(1 for d in guards if d["passed"]), len(guards)),
        },
        "by_class": by_class,
        "detail": details,
    }


# --------------------------------------------------------------------------- verdict

def load_baseline(path: Path | None = None) -> dict | None:
    """Load the pinned floor, or None if there isn't a usable one.

    A CORRUPT baseline and an ABSENT baseline both return None but are not the same
    event, and the old blanket `except Exception: return None` made them indistinguishable
    — a truncated or hand-edited baseline.json silently downgraded the gate to
    "no pinned baseline", which is the one state in which nothing can regress. The caller
    (`_run_gate`) tells them apart by whether the file exists; this reports the specific
    error so the log says which it was and why."""
    path = Path(path or BASELINE_PATH)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[retrieval_eval] baseline at {path} is not valid JSON "
              f"(line {exc.lineno}, col {exc.colno}: {exc.msg}) — the regression floor "
              f"is NOT being applied", file=sys.stderr)
        return None
    except OSError as exc:
        print(f"[retrieval_eval] baseline at {path} is unreadable "
              f"({type(exc).__name__}: {exc}) — the regression floor is NOT being applied",
              file=sys.stderr)
        return None
    if not isinstance(data, dict):
        print(f"[retrieval_eval] baseline at {path} is a {type(data).__name__}, expected an "
              f"object — the regression floor is NOT being applied", file=sys.stderr)
        return None
    return data


def load_class_floors(baseline: dict | None) -> dict[str, float]:
    """Per-class floors, preferring the pinned copy in `baseline.json`'s `_floors` key
    (so a re-pin can raise a floor with evidence, no code change) and falling back to the
    code-level `PER_CLASS_FLOORS` constant when the baseline has none or is unusable —
    an absent or corrupt baseline must not silently disarm the floor gate."""
    if isinstance(baseline, dict):
        pinned = baseline.get("_floors")
        if isinstance(pinned, dict) and pinned:
            return {k: float(v) for k, v in pinned.items()}
    return dict(PER_CLASS_FLOORS)


def verdict(result: dict, baseline: dict | None = None) -> tuple[str, list[str]]:
    """PASS / WARN / FAIL against the pinned baseline.

    FAIL is reserved for the two things that mean the ruler itself or the corpus broke:
    a contamination guard tripping (the fixture is retrievable again) and a real drop below
    the pinned floor. Everything else is WARN — including a *rise*, which is worth a human
    glance because the usual cause of a sudden jump is a question set that drifted."""
    reasons: list[str] = []
    status = PASS

    # A stale expected node ID (usually a renamed document — IDs are slug+hash of title/path)
    # makes its question unanswerable and drags the class rate down exactly like a real
    # regression would. Any regression detected while fixtures are stale is therefore
    # reported as WARN naming the stale IDs, never as FAIL: the remedy is to repair the
    # fixture, and paging the COVENANT inbox about a ranker that did not change is the
    # false-alert class this programme exists to close.
    unresolved: dict = result.get("unresolved") or {}
    if unresolved:
        detail = "; ".join(f"{eid} → {', '.join(ids)}" for eid, ids in sorted(unresolved.items()))
        reasons.append(f"STALE FIXTURE: {len(unresolved)} golden entr"
                       f"{'y' if len(unresolved) == 1 else 'ies'} reference node IDs that are "
                       f"no longer in the index ({detail}). Those questions cannot pass; "
                       f"repair the golden set before reading any rate below as a regression.")
        status = WARN

    contam = result["by_class"].get("contamination")
    if contam and contam["passed"] < contam["n"]:
        status = FAIL
        reasons.append(
            f"CONTAMINATION: {contam['n'] - contam['passed']} of {contam['n']} probes found a "
            f"Brain eval fixture in the pack ({', '.join(contam['failing'])}). The scorecard "
            f"is grading itself again — every retrieval number below is void until this clears.")

    # Per-class ABSOLUTE floors. Unlike the relative tolerances below (which only fire on
    # a DROP from the previous baseline run), a floor breach is a hard FAIL regardless of
    # whether the class moved — a class that has always scored below its floor must not
    # sit there silently forever just because it never regressed further. Stale-fixture
    # entries are excluded from a class's numerator/denominator only insofar as the class
    # rate itself already reflects them (unresolved ids fail their own question); a floor
    # breach caused by stale fixtures still reports STALE FIXTURE above, but the floor
    # itself is not softened to WARN — a floor is a promise about the ranker, and a stale
    # fixture is a reason to fix the fixture, not a reason to let the floor slip.
    floors = load_class_floors(baseline)
    for cls, blk in result["by_class"].items():
        floor = floors.get(cls)
        if floor is None:
            continue
        key = "recall5_rate" if cls in POSITIVE_CLASSES else "pass_rate"
        val = blk.get(key)
        if val is not None and val < floor:
            status = FAIL
            reasons.append(f"CLASS FLOOR BREACH: '{cls}' {key}={val:.3f} is below its pinned "
                           f"floor {floor:.3f}; failing: {', '.join(blk['failing']) or '—'}")

    if baseline is None:
        reasons.append("no pinned baseline — run with --write-baseline to pin one")
        return (WARN if status == PASS else status), reasons

    # A regression measured while fixtures are stale is not attributable to the ranker, so
    # it degrades to WARN. `_regression` is the level every regression check below reports at.
    _regression = WARN if unresolved else FAIL

    base_pos = (baseline.get("positive") or {}).get("recall5_rate")
    now_pos = (result.get("positive") or {}).get("recall5_rate")
    if base_pos is not None and now_pos is not None:
        if now_pos < base_pos - OVERALL_TOLERANCE:
            status = _regression if status != FAIL else FAIL
            reasons.append(f"overall recall@5 regressed {base_pos:.3f} → {now_pos:.3f} "
                           f"(tolerance {OVERALL_TOLERANCE})"
                           + (" — NOT attributed to the ranker: stale fixtures above"
                              if unresolved else ""))
        elif now_pos > base_pos + OVERALL_TOLERANCE:
            if status == PASS:
                status = WARN
            reasons.append(f"overall recall@5 ROSE {base_pos:.3f} → {now_pos:.3f} — confirm a "
                           f"real improvement, then re-pin the baseline")

    for cls, blk in result["by_class"].items():
        base_blk = (baseline.get("by_class") or {}).get(cls) or {}
        key = "recall5_rate" if cls in POSITIVE_CLASSES else "pass_rate"
        b, n = base_blk.get(key), blk.get(key)
        if b is None or n is None:
            continue
        if n < b - PER_CLASS_TOLERANCE:
            status = _regression if status != FAIL else FAIL
            reasons.append(f"class '{cls}' {key} regressed {b:.3f} → {n:.3f} "
                           f"(tolerance {PER_CLASS_TOLERANCE}); failing: "
                           f"{', '.join(blk['failing']) or '—'}")

    guards = result.get("guards") or {}
    base_guards = (baseline.get("guards") or {}).get("pass_rate")
    if base_guards is not None and guards.get("pass_rate") is not None:
        if guards["pass_rate"] < base_guards:
            if status == PASS:
                status = WARN
            reasons.append(f"guard pass rate fell {base_guards:.3f} → {guards['pass_rate']:.3f} "
                           f"— the Brain is answering questions that have no answer")

    return status, reasons


def quality_verdict(result: dict,
                    targets: dict[str, float] | None = None) -> tuple[str, list[str]]:
    """Grade absolute usefulness without consulting or modifying the regression baseline.

    Missing metrics fail closed.  A quality objective that disappears from a result must
    never turn green because the comparison happened to skip it.
    """
    targets = dict(targets or QUALITY_TARGETS)
    reasons: list[str] = []
    for dotted, target in sorted(targets.items()):
        cur = result
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                cur = None
                break
            cur = cur[part]
        if (isinstance(cur, bool) or not isinstance(cur, (int, float)) or
                not math.isfinite(float(cur)) or not 0.0 <= float(cur) <= 1.0):
            reasons.append(f"QUALITY METRIC INVALID: {dotted}={cur!r}; expected finite "
                           f"rate in [0,1], target {target:.3f}")
        elif float(cur) < float(target):
            reasons.append(
                f"QUALITY GAP: {dotted}={float(cur):.3f}, target {float(target):.3f}, "
                f"gap {float(target) - float(cur):.3f}"
            )
    return (FAIL if reasons else PASS), reasons


# --------------------------------------------------------------------------- output

def write_report(result: dict, status: str, reasons: list[str],
                 path: Path | None = None,
                 quality_status: str | None = None,
                 quality_reasons: list[str] | None = None,
                 quality_result: dict | None = None) -> Path:
    """Write the human-readable scorecard.

    NOTE: question TEXT is never written here. Entries are named by id. The whole failure
    this module exists to fix was golden questions living inside an indexed document — and
    `index/` is not indexed today, but a report is exactly the kind of file that gets copied
    into a note. Ids only, always."""
    path = Path(path or REPORT_PATH)
    L = [
        "# Brain — Retrieval Eval",
        "",
        f"_generated: {result['generated']}_",
        "",
        f"## Regression: {_ICON[status]} {status}",
        "",
        f"- questions: **{result['questions']}**",
        f"- positive recall@{RECALL_DEPTH}: **{result['positive']['recall5_rate']}** "
        f"· top1: **{result['positive']['top1_rate']}** (n={result['positive']['n']})",
        f"- precision@{RECALL_DEPTH}: **{result['positive'].get('precision5_rate')}** "
        f"· MRR: **{result['positive'].get('mrr')}** · "
        f"superseded-in-top5: **{result['positive'].get('superseded_in_top5_rate')}** · "
        f"wrong-context: **{result['positive'].get('wrong_context_rate')}**",
        f"- guards (negative + contamination) passing: **{result['guards']['pass_rate']}** "
        f"(n={result['guards']['n']})",
        "",
    ]
    if reasons:
        L += ["### Why", ""] + [f"- {r}" for r in reasons] + [""]
    if quality_status is not None:
        L += [f"## Useful-answer quality: {_ICON[quality_status]} {quality_status}", ""]
        if quality_result:
            L += [f"- development diagnostic questions: **{quality_result['questions']}**",
                  f"- positive recall@{RECALL_DEPTH}: **{quality_result['positive']['recall5_rate']}** "
                  f"· top1: **{quality_result['positive']['top1_rate']}**",
                  f"- guards passing: **{quality_result['guards']['pass_rate']}**", ""]
        if quality_reasons:
            L += [f"- {r}" for r in quality_reasons] + [""]
    L += ["## Per class", "",
          "| Class | n | pass | recall@5 | top1 | failing |", "|---|---|---|---|---|---|"]
    for cls, b in result["by_class"].items():
        L.append(f"| `{cls}` | {b['n']} | {b['passed']}/{b['n']} | "
                 f"{b.get('recall5_rate', '—')} | {b.get('top1_rate', '—')} | "
                 f"{', '.join(b['failing']) or '—'} |")
    L += ["", "## Per question", "", "| id | class | result | top1 node | detail |",
          "|---|---|---|---|---|"]
    for d in result["detail"]:
        L.append(f"| `{d['id']}` | {d['class']} | {'✅' if d['passed'] else '❌'} | "
                 f"`{d.get('top1_id') or '—'}` | {d.get('reason') or ''} |")
    L += ["",
          "_Questions are referenced by id only. The text lives in "
          "`engine/tests/fixtures/golden.jsonl`, which is de-indexed on purpose — see that "
          "directory's README._", ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(L), encoding="utf-8")
    write_score(result, status, quality_status=quality_status,
                quality_result=quality_result)
    return path


def write_score(result: dict, status: str, path: Path | None = None,
                quality_status: str | None = None,
                quality_result: dict | None = None) -> Path | None:
    """Publish the scorecard's headline numbers for `/metrics` to read.

    `/metrics` has carried a ~93% "context savings" KPI since the Brain shipped. That
    number is RELEVANCE-BLIND by construction: it is (nodes returned x an assumed raw file
    size) minus tokens served, so a pack of twenty perfectly irrelevant nodes scores
    BETTER than a pack of three correct ones. Optimising it optimises for returning many
    small wrong things — textbook Goodhart, and it is the only quality-adjacent figure the
    endpoint has ever shown.

    This does not remove it. Removing a number because it flatters is its own dishonesty,
    and throughput is a real thing to know. It puts the honest signal BESIDE it, sourced
    from the gate that already measures it, so whoever reads "savings 93%" reads
    "recall@5 0.474" in the same response.

    Never raises: a metrics sidecar that can fail a scoring run has the priority
    backwards."""
    path = Path(path or SCORE_PATH)
    try:
        payload = {
            "generated": result.get("generated"),
            "status": FAIL if FAIL in (status, quality_status) else status,
            "regression_status": status,
            "quality_status": quality_status,
            "quality": ({
                "questions": quality_result.get("questions"),
                "positive": quality_result.get("positive"),
                "guards": quality_result.get("guards"),
            } if quality_result else None),
            "questions": result.get("questions"),
            "positive": result.get("positive"),
            "guards": result.get("guards"),
            "by_class": {c: {k: v for k, v in b.items() if k != "failing"}
                         for c, b in (result.get("by_class") or {}).items()},
            "unresolved_fixture_ids": sorted(result.get("unresolved") or {}),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return path
    except Exception as exc:  # noqa: BLE001
        print(f"[retrieval_eval] could not write {path}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return None


def _append_inbox(heading: str, lines: list[str]) -> None:
    """Append one governance block to the COVENANT inbox (mirrors coverage_check)."""
    inbox = C.COVENANT_INBOX
    try:
        inbox.parent.mkdir(parents=True, exist_ok=True)
        if not inbox.exists() or inbox.stat().st_size == 0:
            inbox.write_text(
                "---\ncreated: " + now_iso() + "\nowner: COVENANT\nttl_tier: 1\n"
                "status: active\n---\n\n# COVENANT Inbox\n\n## Active\n\n",
                encoding="utf-8",
            )
        with inbox.open("a", encoding="utf-8") as f:
            f.write("\n".join([heading, ""] + lines + [""]) + "\n")
    except OSError as exc:
        # Alerting stays best-effort — the non-zero exit and the written report still
        # stand, and raising here would turn a FAIL into a CRASH and lose the verdict.
        # But it must not be SILENT: a module whose entire purpose is "never fail
        # quietly" cannot have a mute failure path of its own. If the inbox is
        # unwritable, that is itself the alert, and it goes to stderr → the LaunchAgent's
        # .err file. The block that could not be delivered is echoed so it is recoverable
        # from the log.
        print(f"[retrieval_eval] COVENANT inbox at {C.COVENANT_INBOX} is UNWRITABLE "
              f"({type(exc).__name__}: {exc}) — this alert was NOT delivered:",
              file=sys.stderr)
        print("\n".join([heading] + lines), file=sys.stderr)


def _emit_inbox_alert(status: str, result: dict, reasons: list[str]) -> None:
    """Append a governance alert to the COVENANT inbox on FAIL."""
    if status != FAIL:
        return
    lines = [f"- {r}" for r in reasons]
    lines.append(f"    - positive recall@{RECALL_DEPTH}: "
                 f"{result['positive']['recall5_rate']} · guards: "
                 f"{result['guards']['pass_rate']}")
    lines.append("    - source: `index/retrieval_eval.md`")
    _append_inbox(f"### 🚨 Brain Retrieval Eval FAIL — {now_iso()}", lines)


def _emit_crash_alert(exc: BaseException) -> None:
    """Alert when the ruler itself died before producing a verdict.

    Without this, a crash is SILENT to governance: a traceback goes to an unread
    `.err` file, launchd records a non-zero exit nobody reads, and `_emit_inbox_alert`
    never runs — so from the inbox's point of view "the eval didn't run" is
    indistinguishable from "the eval passed". That is the dead-notifier failure class this
    whole programme exists to close, and a gate that can die quietly is not a gate.

    Deliberately a DISTINCT heading from FAIL. FAIL means the Brain got worse; CRASH means
    nothing was measured and every number in the last report is stale. Conflating them would
    send someone hunting a retrieval regression that was really a broken import."""
    import traceback
    tb = traceback.format_exc().strip().splitlines()
    _append_inbox(
        f"### 💥 Brain Retrieval Eval CRASHED — {now_iso()}",
        [f"- The retrieval gate raised `{type(exc).__name__}: {exc}` and produced NO verdict.",
         "- Nothing was measured. The numbers in `index/retrieval_eval.md` are from the "
         "last successful run and must not be quoted as current.",
         "- run: `python3 -m engine.retrieval_eval` · log: "
         "`~/Library/Logs/arcturion/brain-retrieval-eval.err`",
         "",
         "```",
         *tb[-12:],
         "```"],
    )


def write_baseline(result: dict, path: Path | None = None) -> Path:
    """Pin the current numbers as the floor every later leg is measured against."""
    path = Path(path or BASELINE_PATH)
    payload = {
        "pinned": result["generated"],
        "questions": result["questions"],
        "positive": result["positive"],
        "guards": result["guards"],
        "by_class": {c: {k: v for k, v in b.items() if k != "failing"}
                     for c, b in result["by_class"].items()},
        "_floors": dict(PER_CLASS_FLOORS),
        "_note": "Pinned floor for the retrieval-honesty programme. Raise it only with "
                 "evidence of a real improvement; never lower it to make a run green. "
                 "_floors are ABSOLUTE per-class minimums (see verdict()'s class-floor "
                 "check) — edit only with the same evidence discipline.",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _merge_heldout_into_baseline(result: dict, path: Path | None = None) -> Path:
    """Record a held-out measurement's headline numbers into baseline.json's `_heldout`
    key, without touching the gated `positive`/`guards`/`by_class`/`_floors` blocks a
    held-out run never computes a regression floor for. Never invents a baseline file —
    the golden-set gate must be pinned first."""
    path = Path(path or BASELINE_PATH)
    payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    payload["_heldout"] = {
        "file": HELDOUT_PATH.name,
        "n": result["questions"],
        "recall5_rate": result["positive"]["recall5_rate"],
        "top1_rate": result["positive"]["top1_rate"],
        "precision5_rate": result["positive"].get("precision5_rate"),
        "mrr": result["positive"].get("mrr"),
        "guards_pass_rate": result["guards"]["pass_rate"],
        "measured": result["generated"],
        "_note": "Measurement only — never a tuning target. Strict id-exact scoring, "
                 "same rules as the golden set.",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def run_heldout(token_budget: int = 1500, verbose: bool = True) -> dict:
    """Score the held-out set — MEASUREMENT ONLY. No baseline comparison, no verdict, no
    COVENANT alert, and (`--write-baseline` aside) no write. The held-out set exists to
    answer one question the golden set structurally cannot: is a golden-set improvement
    real, or an artefact of ~8 tuning knobs having been tuned against the same 26-57
    questions that grade them? A gate built on top of this file would create exactly the
    incentive to touch it that the whole point of holding it out is to remove."""
    entries = load_golden(HELDOUT_PATH)
    return run(entries=entries, token_budget=token_budget, verbose=verbose)


def _run_gate(args) -> int:
    """The gate proper. Separated from `main` so `main` can be a pure crash boundary."""
    if getattr(args, "heldout", False):
        result = run_heldout(token_budget=args.token_budget, verbose=not args.json)
        merged_path = None
        if args.write_baseline:
            merged_path = _merge_heldout_into_baseline(result)
        if args.json:
            payload = dict(result)
            if merged_path is not None:
                payload["_baseline_merged"] = str(merged_path)
            print(json.dumps(payload, indent=2))
        else:
            print(f"\nHELD-OUT (measurement only, n={result['questions']}, "
                  f"never used to tune): recall@{RECALL_DEPTH}="
                  f"{result['positive']['recall5_rate']} top1={result['positive']['top1_rate']} "
                  f"precision@{RECALL_DEPTH}={result['positive'].get('precision5_rate')} "
                  f"mrr={result['positive'].get('mrr')} "
                  f"guards={result['guards']['pass_rate']}")
            if merged_path is not None:
                print(f"held-out snapshot merged into {merged_path}")
        return 0

    if not args.force:
        busy, why = reindex_in_progress()
        if busy:
            # Deliberately does NOT overwrite index/retrieval_eval.md. The last real
            # scorecard is dated inside and stays the most recent MEASUREMENT; replacing it
            # with a "didn't score" placeholder would destroy the numbers to record a
            # non-event. The skip is loud in the log (which launchd captures) and exits 0 —
            # a scheduling overlap is not a defect, and paging COVENANT for one would train
            # people to ignore the alert that matters.
            print(f"{_ICON[WARN]} WARN — reindex in progress, NOT SCORED ({why}).")
            print("  Scoring a half-rebuilt graph would publish a meaningless verdict; the "
                  "previous report stands. Re-run with --force to score anyway.")
            return 0

    result = run(token_budget=args.token_budget, verbose=not args.json)
    baseline = load_baseline()
    status, reasons = verdict(result, baseline)
    quality_result = run(entries=load_golden(REPRESENTATIVE_QUALITY_PATH),
                         token_budget=args.token_budget, verbose=not args.json)
    quality_status, quality_reasons = quality_verdict(quality_result)
    # An absent baseline and a CORRUPT one both arrive here as None, and both leave the
    # gate unable to detect a regression — but only one of them is a fixable defect.
    # `verdict` already reports the absent case; this names the corrupt one so nobody
    # reads "no pinned baseline" on a run where the file is sitting right there.
    if baseline is None and BASELINE_PATH.exists():
        reasons.insert(0, f"BASELINE UNUSABLE: {BASELINE_PATH} exists but could not be "
                          f"parsed (see stderr) — NOTHING can regress against it. Repair "
                          f"or re-pin it before trusting a PASS.")
        if status == PASS:
            status = WARN
    report = write_report(result, status, reasons,
                          quality_status=quality_status,
                          quality_reasons=quality_reasons,
                          quality_result=quality_result)

    if args.write_baseline:
        bp = write_baseline(result)
        print(f"baseline pinned → {bp}")

    if args.json:
        payload = dict(result)
        payload["regression_status"] = status
        payload["quality_status"] = quality_status
        payload["quality"] = quality_result
        print(json.dumps(payload, indent=2))
    else:
        print(f"\npositive recall@{RECALL_DEPTH}={result['positive']['recall5_rate']} "
              f"top1={result['positive']['top1_rate']} "
              f"guards={result['guards']['pass_rate']}")
        for cls, b in result["by_class"].items():
            print(f"  {cls:<14} pass {b['passed']}/{b['n']}"
                  + (f"  recall@5={b['recall5_rate']} top1={b['top1_rate']}"
                     if "recall5_rate" in b else ""))
        for r in reasons:
            print(f"  · {r}")
        for r in quality_reasons:
            print(f"  · {r}")
        print(f"\nRegression {_ICON[status]} {status}; useful-answer quality "
              f"{_ICON[quality_status]} {quality_status} → {report}")

    if not args.no_alert:
        combined_reasons = reasons + quality_reasons
        _emit_inbox_alert(FAIL if FAIL in (status, quality_status) else status,
                          result, combined_reasons)
    return 1 if FAIL in (status, quality_status) else 0


def main(argv: list[str] | None = None) -> int:
    """Crash boundary around the gate.

    Exit codes are verdicts, not accidents: 0 = PASS/WARN, 1 = FAIL (the Brain got worse),
    2 = CRASH (nothing was measured). A bare traceback would have exited 1 into an unread
    `.err` file and alerted no one — 'didn't run' and 'passed' must never look the same from
    the COVENANT inbox."""
    ap = argparse.ArgumentParser(description="Brain retrieval eval — the honest ruler")
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--write-baseline", action="store_true",
                    help="pin the current numbers as the regression floor")
    ap.add_argument("--no-alert", action="store_true",
                    help="never write to the COVENANT inbox")
    ap.add_argument("--token-budget", type=int, default=1500)
    ap.add_argument("--force", action="store_true",
                    help="score even if a reindex appears to be in progress")
    ap.add_argument("--heldout", action="store_true",
                    help="score engine/tests/fixtures/heldout.jsonl instead — measurement "
                         "only, no verdict, no gate, no COVENANT alert; combine with "
                         "--write-baseline to record the snapshot in baseline.json's "
                         "_heldout key")
    args = ap.parse_args(argv)
    try:
        return _run_gate(args)
    except Exception as exc:            # noqa: BLE001 — a crash boundary catches everything
        import traceback
        traceback.print_exc()
        if not args.no_alert:
            _emit_crash_alert(exc)
        print(f"\n{_ICON[FAIL]} CRASH — no verdict produced; COVENANT inbox alerted")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
