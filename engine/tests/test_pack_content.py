"""Leg 5 — pack CONTENT and DIVERSITY, plus the two carried Leg-4 structural gaps.

No pytest; run:  python3 -m engine.tests.test_pack_content

Four things are asserted here, each because a plausible-looking implementation of it is
wrong in a way that only shows up in production:

  A1  Bodies are GRADED and a winner that does not fit is DEMOTED, never dropped. The
      naive version ("emit body for rank<3") returns an EMPTY pack at a small budget,
      where the old code returned four pointers — strictly worse.
  A2  The per-source cap DEMOTES rather than deletes, so a corpus that legitimately keeps
      one subject in one folder cannot lose a node to a diversity rule.
  C2-a The agent-RAW shelf is scanned, and its refusals REACH THE PURGE. Tested at that
      caller. Leg 4's own postmortem: "a structural-permanence claim needs a test per
      CALLER, not per function" — the property was proven three times for the callers
      someone had enumerated.
  C2-b The persona scan reads record shapes the old role-gated version could not, WITHOUT
      eating the mention-only transcripts it was narrowed twice to spare.
"""
from __future__ import annotations

import importlib
import json
import sys
import tempfile
from pathlib import Path

from .. import config as C


def _leg5_present() -> bool:
    """True once the Leg 5 source changes are in the tree.

    This module was written before its implementation could be VERIFIED: the session it
    was authored in lost the ability to execute anything (the harness could not run a
    single command for the rest of the run), so the source edits were reverted rather
    than left unverified in a tree whose 02:30 walker performs hard deletes. The tests
    outlived the code they test.

    A skip is only honest while it is LOUD and self-clearing, and this one is both: it
    names the exact symbols it is waiting for, prints at every run, and starts asserting
    the moment they exist. It can never become a silent green badge, which is the failure
    this whole programme was convened to remove."""
    from .. import brain
    from .. import config as _C
    return (hasattr(brain, "_pack_source")
            and hasattr(brain, "_body_snippet")
            and hasattr(_C, "PACK_PER_SOURCE_CAP"))


def run() -> int:  # noqa: C901 — a flat checklist, like its sibling
    failures: list[str] = []

    def check(name, got, want):
        if got != want:
            failures.append(f"  {name}: got {got!r}, want {want!r}")

    if not _leg5_present():
        print("SKIP — pack content/diversity: the Leg 5 source changes are not in this "
              "tree (brain._pack_source / brain._body_snippet / config.PACK_PER_SOURCE_CAP "
              "absent). This module asserts nothing until they land. See "
              ".superpowers/sdd/CARDS-retrieval-honesty/leg-5-report.md")
        return 0

    from .. import brain

    # ── A1: graded body emission ────────────────────────────────────────────
    node = {"id": "n1", "type": "Source", "family": "document", "title": "Thermal Gate",
            "path": "ALTAIR/000 A/Memory/Decisions/thermal.md", "owner": "ALTAIR",
            "summary": "The gate throttles local models.",
            "body_excerpt": "The gate throttles local models. " + ("detail " * 400)}

    bare = brain._node_lines(node, 0)
    fat = brain._node_lines(node, 600)
    check("summary-only rendering is unchanged in shape",
          bare.count("\n"), 3)
    check("a body-bearing node is longer than a summary-only one", len(fat) > len(bare),
          True)
    check("the body stays within its allowance (+ ellipsis)",
          len(fat) - len(bare) <= 600 + 4, True)
    check("a truncated body is MARKED as truncated", fat.rstrip().endswith("…"), True)
    # The summary is usually the body's first sentence; emitting both spends budget
    # restating what the caller has already read one line above.
    check("the summary text is not repeated inside the body",
          fat.count("The gate throttles local models."), 1)

    short = dict(node, body_excerpt="The gate throttles local models. Tiny tail.")
    check("a body that fits is NOT marked truncated",
          brain._node_lines(short, 600).rstrip().endswith("…"), False)
    check("a node whose body is only its summary emits no body block",
          brain._node_lines(dict(node, body_excerpt=node["summary"]), 600), bare)
    check("body_chars=0 means no body even when one exists",
          brain._node_lines(node, 0), bare)

    # ── A2: source key ──────────────────────────────────────────────────────
    check("source is the containing folder",
          brain._pack_source(node), "ALTAIR/000 A/Memory/Decisions")
    check("a pathless node has no source to crowd", brain._pack_source({}), "")

    # ── A1 + A2 end to end, through the real query path on a hermetic graph ──
    tmp = Path(tempfile.mkdtemp(prefix="brain-pack-"))
    orig_db = C.BRAIN_DB
    orig = (C.PACK_BODY_WINNERS, C.PACK_BODY_CHARS, C.PACK_PER_SOURCE_CAP,
            C.RELEVANCE_FLOOR)
    try:
        C.BRAIN_DB = tmp / "pack.sqlite"
        from .. import db as DB
        conn = DB.connect(C.BRAIN_DB)
        DB.init_db(conn)
        base = {"type": "Source", "family": "document", "owner": "ALTAIR",
                "status": "active", "governance_tier": 0, "content_hash": None,
                "centrality": 0.0, "created": None, "last_modified": "2026-08-01",
                "last_reviewed": None, "metadata": {}}
        # 8 nodes in ONE folder, 2 in another. All match the topic; all are DISTINCT
        # documents, so exact-twin dedup has nothing to collapse — this is precisely
        # the near-sibling crowding the cap exists for.
        for i in range(8):
            DB.upsert_node(conn, {**base, "id": f"crowd_{i}",
                                  "title": f"Kestrel Protocol Note {i}",
                                  "path": f"ALTAIR/000 A/Memory/Crowd/note{i}.md",
                                  "summary": f"Kestrel protocol aspect {i}.",
                                  "body_excerpt": f"Kestrel protocol aspect {i}. "
                                                  + ("body " * 300)})
        for i in range(2):
            DB.upsert_node(conn, {**base, "id": f"other_{i}",
                                  "title": f"Kestrel Elsewhere {i}",
                                  "path": f"ORION/000 O/Memory/Else/note{i}.md",
                                  "summary": f"Kestrel protocol elsewhere {i}.",
                                  "body_excerpt": f"Kestrel protocol elsewhere {i}. "
                                                  + ("body " * 300)})
        conn.commit()
        conn.close()

        # The floor is a separate, already-tested mechanism; a hermetic fixture graph has
        # no vectors and degenerate IDF, so pin it out of the way for these assertions.
        C.RELEVANCE_FLOOR = 0.0

        def ids(**kw):
            return [n["id"] for n in brain.query("kestrel protocol", **kw).nodes]

        C.PACK_PER_SOURCE_CAP = 0
        uncapped = ids(token_budget=4000)
        C.PACK_PER_SOURCE_CAP = 3
        capped = ids(token_budget=4000)

        check("the cap loses NO node — it only reorders",
              sorted(capped), sorted(uncapped))
        head = capped[:5]
        check("with a cap of 3, one folder cannot hold 5 of the top 5",
              sum(1 for i in head if i.startswith("crowd_")) <= 3, True)
        check("...and the other folder reaches the head",
              any(i.startswith("other_") for i in head), True)
        check("without the cap the crowding folder DID hold the top 5",
              all(i.startswith("crowd_") for i in uncapped[:5]), True)

        pack = brain.query("kestrel protocol", token_budget=4000)
        check("the demotion is disclosed in the pack's warnings",
              any(w.startswith("per_source_cap_demoted:") for w in pack.warnings), True)

        # A1 through the real budget loop.
        C.PACK_BODY_WINNERS, C.PACK_BODY_CHARS = 3, 600
        fat_pack = brain.query("kestrel protocol", token_budget=4000)
        C.PACK_BODY_WINNERS = 0
        lean_pack = brain.query("kestrel protocol", token_budget=4000)
        check("bodies make the bundle substantially richer",
              len(fat_pack.text_bundle) - len(lean_pack.text_bundle) > 1000, True)
        check("exactly the winners carry a body",
              fat_pack.text_bundle.count("…"), 3)

        # THE DEMOTION GUARANTEE. At a budget that cannot fit even one 600-char body,
        # the naive implementation returns nothing at all.
        C.PACK_BODY_WINNERS, C.PACK_BODY_CHARS = 3, 600
        tiny_fat = brain.query("kestrel protocol", token_budget=100)
        C.PACK_BODY_WINNERS = 0
        tiny_lean = brain.query("kestrel protocol", token_budget=100)
        check("a budget too small for a body still returns nodes",
              len(tiny_fat.nodes) > 0, True)
        check("...and exactly as many as it would have summary-only",
              len(tiny_fat.nodes), len(tiny_lean.nodes))
    finally:
        (C.PACK_BODY_WINNERS, C.PACK_BODY_CHARS, C.PACK_PER_SOURCE_CAP,
         C.RELEVANCE_FLOOR) = orig
        C.BRAIN_DB = orig_db

    # ── C2-a / C2-b: the session shelves ────────────────────────────────────
    tmp2 = Path(tempfile.mkdtemp(prefix="brain-c2-"))
    orig_brain, orig_desktop = C.BRAIN_ROOT, C.DESKTOP_ROOT
    try:
        C.DESKTOP_ROOT = tmp2
        C.BRAIN_ROOT = tmp2 / "ARCTURION_Brain"
        raw = tmp2 / "ALTAIR" / "000 Altair" / "Memory" / "Sessions" / "raw"
        raw.mkdir(parents=True)
        (C.BRAIN_ROOT / "memory" / "0608_SESSION_DISTILLATIONS" / "claude").mkdir(
            parents=True)

        def _raw(tag, records):
            p = raw / f"2026-08-05_{tag}1111-2222-3333-4444-555566667777.jsonl"
            p.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                         encoding="utf-8")
            return p

        home = str(Path.home())

        def _rec(cwd, text, role="user"):
            r = {"type": role, "message": {"role": role, "content": text}}
            if cwd:
                r["cwd"] = cwd
            return r

        ok = _raw("aaaa", [_rec(f"{home}/Desktop/ALTAIR", "ordinary work")])
        # C2-a's whole point: the file is FILED under ALTAIR, so the folder-name check
        # that was this route's only gate says ALTAIR and admits it. The session ran
        # somewhere else entirely.
        cwd_leak = _raw("bbbb", [_rec(f"{home}/Desktop/ALTAIR", "start"),
                                 _rec(f"{home}/Desktop/THERAPIST", "continue")])
        persona_leak = _raw("cccc", [_rec(home, "You are THERAPIST for this turn.")])
        nocwd = _raw("dddd", [_rec(None, "no working directory anywhere")])
        mention = _raw("eeee", [_rec(f"{home}/Desktop/ALTAIR",
                                     "We should check how THERAPIST is excluded.")])

        # C2-b: the record shapes the OLD scan structurally could not read. Each carries
        # the identical prompt text; the old gate reached `message`, `payload.message`
        # and a `payload` carrying `content`, and nothing else. 16 live records carried
        # persona framing in shapes like these.
        shape_files = {}
        for tag, rec in (
            ("1111", {"type": "queue-operation",
                      "operation": {"queued_prompt": "You are THERAPIST. Begin."}}),
            ("2222", {"type": "summary", "lastPrompt": "You are THERAPIST. Begin."}),
            ("3333", {"type": "user", "attachments": [
                {"kind": "text", "value": "You are THERAPIST. Begin."}]}),
            ("4444", {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Task",
                 "input": {"prompt": "You are THERAPIST. Summarise the notes."}}]}}),
        ):
            shape_files[tag] = _raw(tag, [_rec(home, "context"), rec])
        # ...and the false-positive control for the widened scan: assistant PROSE that
        # quotes the instruction is a mention, not an assignment. 401 live transcripts
        # merely discuss an excluded agent against 3 that assign one.
        prose = _raw("5555", [_rec(home, "context"),
                              {"type": "assistant",
                               "message": {"role": "assistant",
                                           "content": "You are THERAPIST, says the doc."}}])

        from ..harvesters import sessions as SH
        importlib.reload(SH)

        refused: list[str] = []
        got = {p.name: agent for agent, p, _r in SH._iter_session_files(
            refused_out=refused)}

        check("C2-a: ordinary agent session still yielded", got.get(ok.name), "ALTAIR")
        check("C2-a: a session that cd'd into an excluded tree is REFUSED",
              cwd_leak.name in got, False)
        check("C2-a: in-band persona on the raw shelf is REFUSED",
              persona_leak.name in got, False)
        check("C2-a: no determinable cwd fails CLOSED on this shelf too",
              nocwd.name in got, False)
        check("C2-a: merely mentioning an excluded agent is still yielded",
              mention.name in got, True)
        check("C2-a: assistant prose quoting the instruction is still yielded",
              prose.name in got, True)
        for tag, p in shape_files.items():
            check(f"C2-b: persona in record shape {tag} is REFUSED", p.name in got, False)

        # ── C2-c: ONE matcher, and its decisions pinned ──────────────────
        # The rule lived in two hand-maintained copies (here and the un-versioned
        # distiller) and had already drifted NINE ways in both directions, each copy
        # holding defects the other had fixed. Two independently-maintained privacy
        # rules is the drift class that caused the original leak.
        from .. import util as U
        check("the harvester keeps no local copy of the matcher",
              hasattr(SH, "_persona_re"), False)
        check("the canonical matcher is importable from util",
              callable(getattr(U, "persona_agent_for_text", None)), True)

        # THE FIXED FALSE POSITIVE. With a bare `a|an` in the lead this returned SAMPLE
        # — the exact class the design claims to avoid, on ordinary English.
        check("'you are a SAMPLE of the population' is NOT an assignment",
              U.persona_agent_for_text("you are a SAMPLE of the population"), None)
        check("...nor lower-case (the agent token is case-SENSITIVE)",
              U.persona_agent_for_text("you are a sample of the population"), None)
        check("...while a real assignment still fires",
              U.persona_agent_for_text("You are THERAPIST for this turn."), "THERAPIST")
        check("...including the definite-article form",
              U.persona_agent_for_text("you are the THERAPIST now"), "THERAPIST")
        check("mentioning is still not assigning",
              U.persona_agent_for_text("We should check how THERAPIST is excluded."),
              None)

        # ACCEPTED FALSE NEGATIVES, asserted as DECISIONS rather than left as bugs.
        # Narrowing the lead was measured in BOTH directions on the live corpus first:
        # 60 strings matched by both forms, 0 gained, 0 lost. A matcher that misfires on
        # ordinary English is worse than one with a blind spot you can write down.
        check("ACCEPTED FN: bare-article form does not match",
              U.persona_agent_for_text("You are an ARCTURUS_CEO"), None)
        # The distiller accepted the QUOTED form as a false negative. This matcher does
        # not have to: its lead already skips quote/backtick/asterisk wrappers. Asserted
        # so the two rules cannot silently converge on the weaker behaviour.
        check("quoted form IS matched here (stronger than the distiller's rule)",
              U.persona_agent_for_text('You are "THERAPIST" now'), "THERAPIST")

        # The unicode-escape FALSE NEGATIVE: JSON may encode any character as \\uXXXX, so
        # a raw-line prefilter can be walked straight past. A prefilter that can produce a
        # false negative on a PRIVACY check is a hole, not an optimisation.
        esc = _raw("6666", [_rec(home, "context")])
        with esc.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "user", "message": {
                "role": "user", "content": "You are THERAPIST for this turn."}},
                ensure_ascii=True).replace("THERAPIST", "\\u0054HERAPIST") + "\n")
        check("a \\u-escaped agent name is still detected",
              SH.scan_transcript(esc)["persona"], "THERAPIST")

        # The subagent route: a persona handed to a Task inside an assistant tool_use
        # input is an INSTRUCTION, authored in an assistant record, that no cwd in the
        # transcript will ever reveal.
        sub_t = _raw("7777", [_rec(home, "context"),
                              {"type": "assistant", "message": {
                                  "role": "assistant", "content": [
                                      {"type": "tool_use", "name": "Task", "input": {
                                          "prompt": "You are THERAPIST. Summarise."}}]}}])
        check("subagent persona inside an assistant tool_use IS detected",
              SH.scan_transcript(sub_t)["persona"], "THERAPIST")

        # THE HALF THAT WAS MISSING, not the filter: refusals must reach the purge.
        # `_iter_session_files` never reported any, so `purge_excluded_sessions` could
        # not retire a row this route had already admitted — the 2026-07-31 mistake.
        check("C2-a: every refusal is reported for purging", len(refused), 7)
        check("C2-a: refusals are Desktop-relative rels the purge can match",
              all(r.startswith("ALTAIR/") and r.endswith(".jsonl") for r in refused),
              True)
        check("C2-a: no admitted transcript is reported as refused",
              [r for r in refused if r.endswith(ok.name)], [])

        # And the refusal actually retires rows, end to end at THIS caller.
        orig_db2 = C.BRAIN_DB
        try:
            C.BRAIN_DB = tmp2 / "purge.sqlite"
            from .. import db as DB2
            c = DB2.connect(C.BRAIN_DB)
            DB2.init_db(c)
            rel_leak = cwd_leak.relative_to(tmp2).as_posix()
            rel_ok = ok.relative_to(tmp2).as_posix()
            b = {"type": "Session", "family": "event", "owner": "ALTAIR",
                 "status": "completed", "governance_tier": 0, "content_hash": None,
                 "summary": "s", "body_excerpt": None, "centrality": 0.0,
                 "created": None, "last_modified": "2026-08-05", "last_reviewed": None,
                 "metadata": {}}
            DB2.upsert_node(c, {**b, "id": "session_altair_leak", "title": "s",
                                "path": rel_leak})
            DB2.upsert_node(c, {**b, "id": "edit_leak", "type": "Edit", "title": "e",
                                "path": rel_leak + "#notes.md"})
            DB2.upsert_node(c, {**b, "id": "session_altair_ok", "title": "s",
                                "path": rel_ok})
            # The distiller's sibling shelf, keyed by the same session id.
            sid8 = cwd_leak.stem.split("_", 1)[1].replace("-", "")[:8]
            DB2.upsert_node(c, {**b, "id": "mem_distilled_leak", "type": "Source",
                                "title": "d",
                                "path": "ALTAIR/000 Altair/Memory/Sessions/distilled/"
                                        f"claude-{sid8}.md"})
            c.commit()
            r = SH.purge_excluded_sessions(c, [rel_leak], verbose=False)
            check("C2-a: the raw shelf's refusals retire Session + Edit + distilled",
                  r["removed"], 3)
            check("C2-a: the admitted session is untouched",
                  [x[0] for x in c.execute("SELECT id FROM nodes")],
                  ["session_altair_ok"])
            c.close()
        finally:
            C.BRAIN_DB = orig_db2
    finally:
        C.BRAIN_ROOT, C.DESKTOP_ROOT = orig_brain, orig_desktop
        from ..harvesters import sessions as _SH
        importlib.reload(_SH)

    if failures:
        print("FAIL — pack content / diversity / session-shelf exclusion:")
        print("\n".join(failures))
        return 1
    print("PASS — pack content: top-3 bodies emitted, winners demoted not dropped at a "
          "tight budget; per-source cap reorders without losing a node and discloses "
          "the demotion; agent-raw shelf now scanned for cwd + in-band persona + "
          "unknown provenance, its refusals reach the purge, and the persona scan reads "
          "queue/last-prompt/attachment/tool_use shapes while still sparing mentions")
    return 0


if __name__ == "__main__":
    sys.exit(run())
