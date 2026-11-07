"""Hermetic tests for the EXCLUDED_AGENTS chokepoint and the nightly-GC safety
affordances (Leg 4 fix round 1, no pytest).

The first cut of Leg 4 put the exclusion in `util.should_skip_path` and called it done.
It was not done, and the gap is the exact class the leg exists to close — structural
permanence. Two holes:

  * `walker.walk_paths`' single-FILE branch never called any skip check, so
    `python3 -m engine.ingest <excluded file>` indexed it outright. It only LOOKED safe
    because `watch.py::watch_dirs()` independently globs CONSTELLATION_AGENTS folders —
    incidental cover, not enforcement.
  * a walk-root-relative path cannot see its own root: `walk_root(~/Desktop/THERAPIST)`
    yields `02 Mental Health/x.md`, in which the agent name never appears.

So the headline test here drives `ingest_paths` END TO END against an excluded file and
asserts nothing lands in the database. Anything less re-tests the assumption that failed.

Run:  python3 -m engine.tests.test_exclusion_chokepoint
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

from .. import config as C


_DOC = ("---\ntitle: Private Session Note\ntype: memory\nowner: THERAPIST\n---\n\n"
        + "Deeply personal therapy content that must never enter the shared index. " * 8)


def run() -> int:  # noqa: C901 — a checklist, deliberately flat
    failures: list[str] = []

    def check(name, got, want):
        if got != want:
            failures.append(f"  {name}: got {got!r}, want {want!r}")

    tmp = Path(tempfile.mkdtemp(prefix="brain-excl-"))
    orig_db, orig_desktop = C.BRAIN_DB, C.DESKTOP_ROOT
    try:
        # A fake Desktop so absolute paths reduce to "THERAPIST/..." exactly as
        # nodes.path would store them.
        C.DESKTOP_ROOT = tmp
        C.BRAIN_DB = tmp / "brain.sqlite"

        priv_dir = tmp / "THERAPIST" / "06 Sessions"
        priv_dir.mkdir(parents=True)
        priv = priv_dir / "session-2026-08-05.md"
        priv.write_text(_DOC, encoding="utf-8")

        pub_dir = tmp / "ALTAIR" / "000 Altair" / "Memory" / "Decisions"
        pub_dir.mkdir(parents=True)
        pub = pub_dir / "launchd-absolute-paths.md"
        pub.write_text(_DOC.replace("THERAPIST", "ALTAIR"), encoding="utf-8")

        # ── B-1a: ingest_paths on the excluded FILE directly ─────────────────
        from .. import ingest as IN
        stats = IN.ingest_paths([str(priv)], embed=False, verbose=False)
        check("single excluded FILE: nothing walked", stats["files"], 0)
        check("single excluded FILE: nothing upserted", stats["upserted"], 0)

        conn = sqlite3.connect(str(C.BRAIN_DB))
        conn.row_factory = sqlite3.Row
        n = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        check("single excluded FILE: 0 rows in the database", n, 0)

        # The refusal must NOT be recorded in the admission skip ledger. A privacy
        # exclusion is not an admission decision, and writing the path into a
        # queryable ledger would leak the very thing being excluded.
        try:
            led = conn.execute("SELECT COUNT(*) FROM admission_skips").fetchone()[0]
        except sqlite3.OperationalError:
            led = 0
        check("excluded path is NOT written to the skip ledger", led, 0)
        conn.close()

        # ── B-1b: ingest_paths on the excluded agent's DIRECTORY ─────────────
        stats = IN.ingest_paths([str(tmp / "THERAPIST")], embed=False, verbose=False)
        check("excluded DIRECTORY: nothing walked", stats["files"], 0)
        check("excluded DIRECTORY: nothing upserted", stats["upserted"], 0)

        # ── B-1c: the same call on a NON-excluded file still works ───────────
        # Without this the two assertions above pass for a broken walker.
        stats = IN.ingest_paths([str(pub)], embed=False, verbose=False)
        check("control: non-excluded file IS walked", stats["files"], 1)
        check("control: non-excluded file IS upserted", stats["upserted"], 1)

        conn = sqlite3.connect(str(C.BRAIN_DB))
        conn.row_factory = sqlite3.Row
        paths = [r[0] for r in conn.execute("SELECT path FROM nodes")]
        check("control: exactly the ALTAIR row landed", len(paths), 1)
        check("no THERAPIST path anywhere in the index",
              [p for p in paths if "THERAPIST" in str(p)], [])
        conn.close()

        # ── B-1d: walk_root handed the excluded root directly ────────────────
        from .. import walker
        check("walk_root(excluded home) yields nothing",
              list(walker.walk_root(tmp / "THERAPIST")), [])
        check("walk_paths([excluded file]) yields nothing",
              list(walker.walk_paths([priv])), [])
    finally:
        C.BRAIN_DB, C.DESKTOP_ROOT = orig_db, orig_desktop

    # ── D-1: the SESSION HARVESTER route ────────────────────────────────────
    # A second way into the index that NO path check can guard.
    # `agent-session-autosave.sh` copies EVERY live session into the Brain's 0608 shelf
    # regardless of where it ran, so a session with cwd `~/Desktop/THERAPIST` lands at
    # `ARCTURION_Brain/memory/0608_.../<date>_<sid>.jsonl` — a path with nothing
    # excluded-agent-shaped about it. `should_skip_path` sees nothing to refuse, the
    # harvester indexes the transcript's FIRST PROMPT as the Session node's summary, and
    # the nightly sweep would not remove it either because the stored path is not owned
    # by an excluded agent. Latent (zero live rows) — which is exactly what the
    # 2026-07-31 purge was until the next reindex ran.
    #
    # Tested at THIS caller, per the round-1 postmortem: testing the shared path helper
    # again would have missed this entirely, because the helper is not on this route.
    import importlib
    import json
    tmp4 = Path(tempfile.mkdtemp(prefix="brain-sess-"))
    orig_brain, orig_desktop = C.BRAIN_ROOT, C.DESKTOP_ROOT
    try:
        C.DESKTOP_ROOT = tmp4
        C.BRAIN_ROOT = tmp4 / "ARCTURION_Brain"
        shelf = C.BRAIN_ROOT / "memory" / "0608_SESSION_DISTILLATIONS" / "claude"
        shelf.mkdir(parents=True)

        def _transcript(name, cwds, text="ordinary work content", role="user",
                        shape="claude"):
            """`shape` picks the on-disk record format. Both are real: Claude Code puts
            `cwd` at the top level; Codex `rollout-*.jsonl` wraps every record as
            {timestamp, type, payload} and carries it at `payload.cwd`."""
            p = shelf / name
            with p.open("w", encoding="utf-8") as fh:
                for cwd in (cwds or [None]):
                    if shape == "claude":
                        rec = {"type": role, "message": {"role": role, "content": text}}
                        if cwd:
                            rec["cwd"] = cwd
                    else:
                        pay = {"role": role, "content": text}
                        if cwd:
                            pay["cwd"] = cwd
                        rec = {"timestamp": "2026-08-05T00:00:00Z", "type": role,
                               "payload": pay}
                    fh.write(json.dumps(rec) + "\n")
            return p

        def _sid(tag):
            return f"2026-08-05_{tag}1111-2222-3333-4444-555566667777.jsonl"

        home = str(Path.home())
        priv_t = _transcript(_sid("aaaa"), [f"{home}/Desktop/THERAPIST"])
        pub_t = _transcript(_sid("bbbb"), [f"{home}/Desktop/ALTAIR"])
        # The case an early-exit scan misses: starts in ALTAIR, cds into THERAPIST.
        drift_t = _transcript(_sid("cccc"),
                              [f"{home}/Desktop/ALTAIR", f"{home}/Desktop/THERAPIST"])
        hq_t = _transcript(_sid("dddd"), [f"{home}/Desktop/ARCTURION_HQ"])

        # ── C-1: persona assigned IN-BAND ────────────────────────────────
        # The bridge middleware sets the persona in the prompt with `cwd` left at the
        # home directory, so the working-directory question — "where did this run?" —
        # returns nothing and the transcript was harvested as owner HQ. Three such rows
        # were live in the index, retrievable by keyword AND vector search. The cwd
        # filter cannot see this class; only "what was it told to be?" can.
        persona_t = _transcript(_sid("eeee"), [home],
                                text="You are THERAPIST for this turn. Begin.")
        # ...and the three ways that check must NOT overfire, because on the live shelf
        # 3 transcripts are assignments and 401 merely mention an excluded agent:
        mention_t = _transcript(_sid("ffff"), [home],
                                text="We should check how THERAPIST is excluded here.")
        assistant_t = _transcript(_sid("9999"), [home], role="assistant",
                                  text="You are THERAPIST, is what the doc says.")
        lowercase_t = _transcript(_sid("8888"), [home],
                                  text="you are a sample of the population")
        # ── fail closed when provenance is genuinely unknown ─────────────
        nocwd_t = _transcript(_sid("7777"), [], text="no working directory at all")
        # ── the Codex record shape must be READ, not treated as unknown ──
        codex_ok_t = _transcript(_sid("6666"), [f"{home}/Desktop/ALTAIR"], shape="codex")
        codex_bad_t = _transcript(_sid("5555"), [f"{home}/Desktop/THERAPIST"],
                                  shape="codex")

        from ..harvesters import sessions as SH
        importlib.reload(SH)          # _DISTILL_ROOT is module-level, bound at import
        got = {p.name: agent for agent, p, _rel in SH._iter_distillation_files()}
        check("excluded-cwd transcript is NOT yielded", priv_t.name in got, False)
        check("cd-into-excluded transcript is NOT yielded", drift_t.name in got, False)
        check("ALTAIR transcript still yielded", got.get(pub_t.name), "ALTAIR")
        check("HQ-level transcript still yielded", got.get(hq_t.name), "HQ")

        check("C-1: in-band persona is NOT yielded", persona_t.name in got, False)
        check("C-1: merely MENTIONING the agent IS yielded", mention_t.name in got, True)
        check("C-1: assistant-authored text is not an assignment",
              assistant_t.name in got, True)
        check("C-1: 'you are a sample of…' is not an assignment (case-sensitive name)",
              lowercase_t.name in got, True)
        check("fail closed: no determinable cwd is NOT yielded",
              nocwd_t.name in got, False)
        check("Codex payload.cwd is read, not treated as unknown",
              got.get(codex_ok_t.name), "ALTAIR")
        check("Codex payload.cwd excludes correctly", codex_bad_t.name in got, False)
        check("exactly the six safe transcripts survive", len(got), 6)

        # The scan reports the three facts separately: "cwd was the home directory" and
        # "there was no cwd at all" are different, and only the second is fail-closed.
        s = SH.scan_transcript(persona_t)
        check("scan: persona detected", s["persona"], "THERAPIST")
        check("scan: home dir counts as a KNOWN cwd", s["saw_cwd"], True)
        check("scan: home dir resolves to no agent", s["cwd_agents"], [])
        check("scan: no-cwd transcript reports saw_cwd False",
              SH.scan_transcript(nocwd_t)["saw_cwd"], False)
        # The whole-file scan is what catches the cd-into case; an early exit on the
        # first constellation match would report ALTAIR and never see THERAPIST.
        check("_cwd_agents reads the WHOLE file, not just the first match",
              SH._cwd_agents(drift_t), ["ALTAIR", "THERAPIST"])

        # ...and the refused rel-paths are reported to the caller so they can be purged.
        refused: list = []
        list(SH._iter_distillation_files(refused_out=refused))
        # cwd-excluded, cd-into-excluded, in-band persona, no-cwd, Codex-shape excluded.
        check("every refusal route is reported for purging", len(refused), 5)

        # THE OTHER HALF, and the one that matters: a filter only stops the NEXT row.
        # Rows an earlier harvest already created must be RETIRED, every pass — otherwise
        # this is the 2026-07-31 DB-purge mistake in a new costume. Not hypothetical: the
        # first live run of this filter refused 16 transcripts, 25 of whose rows were
        # already in the index, one summarised as the Founder's opening line to his
        # therapist.
        orig_db2 = C.BRAIN_DB
        try:
            C.BRAIN_DB = tmp4 / "purge.sqlite"
            from .. import db as DB2
            c4 = DB2.connect(C.BRAIN_DB)
            DB2.init_db(c4)
            rel_priv = priv_t.relative_to(tmp4).as_posix()
            rel_pub = pub_t.relative_to(tmp4).as_posix()
            base = {"type": "Session", "family": "event", "owner": "HQ",
                    "status": "completed", "governance_tier": 0, "content_hash": None,
                    "summary": "private", "body_excerpt": None, "centrality": 0.0,
                    "created": None, "last_modified": "2026-08-05",
                    "last_reviewed": None, "metadata": {}}
            DB2.upsert_node(c4, {**base, "id": "session_hq_aaaa", "title": "s",
                                 "path": rel_priv})
            # the Edit-node form: "<transcript rel>#<file>"
            DB2.upsert_node(c4, {**base, "id": "edit_aaaa_1", "type": "Edit",
                                 "title": "e", "path": rel_priv + "#notes.md"})
            DB2.upsert_node(c4, {**base, "id": "session_altair_bbbb", "title": "s",
                                 "path": rel_pub})
            # Route 3: the DISTILLED .md the Leg-3 distiller writes beside the transcript.
            # Walked like any other markdown; its path and its `owner: HQ / type:
            # session-log` frontmatter give no hint where the session ran. Only the
            # session id ties it back. 13 such nodes were live when this was written.
            sid8 = priv_t.stem.split("_", 1)[1].replace("-", "")[:8]
            DB2.upsert_node(c4, {
                **base, "id": "mem_distilled_priv", "type": "Source", "title": "d",
                "path": f"ARCTURION_Brain/memory/0608_SESSION_DISTILLATIONS/claude/"
                        f"claude-{sid8}.md"})
            # The distiller's PRIMARY shelf. `%SESSION_DISTILLATIONS%` never matched it,
            # so the purge could not look at the larger of the two shelves (1,259 indexed
            # nodes). No excluded instance lives there today — but the purge is the thing
            # standing between latent and live.
            DB2.upsert_node(c4, {
                **base, "id": "mem_distilled_agent_shelf", "type": "Source", "title": "d",
                "path": f"ALTAIR/000 Altair/Memory/Sessions/distilled/claude-{sid8}.md"})
            # A same-named node OUTSIDE any distillation shelf must survive — the 8-hex id
            # must not collide its way into deleting something unrelated.
            DB2.upsert_node(c4, {**base, "id": "mem_unrelated", "type": "Source",
                                 "title": "u",
                                 "path": f"ALTAIR/notes/{sid8}-unrelated.md"})
            DB2.insert_edge(c4, {"from": "session_hq_aaaa", "to": "edit_aaaa_1",
                                 "predicate": "produced", "family": "structure",
                                 "asserted_by": "T"})
            c4.commit()
            r = SH.purge_excluded_sessions(c4, [rel_priv], verbose=False)
            check("purge covers Session + Edit + BOTH distilled shelves",
                  r["removed"], 4)
            check("purge leaves the non-excluded session and the unrelated node alone",
                  sorted(x[0] for x in c4.execute("SELECT id FROM nodes")),
                  ["mem_unrelated", "session_altair_bbbb"])
            check("purge takes the edges with them",
                  c4.execute("SELECT COUNT(*) FROM edges").fetchone()[0], 0)
            check("purge is idempotent",
                  SH.purge_excluded_sessions(c4, [rel_priv], verbose=False)["removed"], 0)
            c4.close()
        finally:
            C.BRAIN_DB = orig_db2
    finally:
        C.BRAIN_ROOT, C.DESKTOP_ROOT = orig_brain, orig_desktop
        from ..harvesters import sessions as _SH
        importlib.reload(_SH)

    # ── B-3: the exclusion is ANCHORED, not "any component at any depth" ─────
    from ..util import excluded_agent_for_path, should_skip_path
    anchored_excluded = [
        "THERAPIST/06 Sessions/note.md",                                # agent home
        "01 🛎 LOBBY & DIRECTORY/0104_AGENT_IDENTITIES/THERAPIST/soul.md",
        "01_IDENTITY/0104_AGENT_IDENTITIES/THERAPIST/soul.md",
        "ATLAS/15. 🧬 Universe & Agentic HQ/SAMPLE/03 ⭐ Domain 2/x/🏠 home.md",
        "04_CONSTELLATION_AGENTS/ARCTURUS_CEO/memory/chat-sessions.json",
        "03 🛰️ AGENTS/ARCTURUS_CEO/dashboard/data/v2.json",
        "04 🛠️ SKILLS/agent_specialized/ARCTURUS_CEO/README.md",
    ]
    anchored_kept = [
        # THE B-3 CASE. `SAMPLE` is a generic folder name; an unanchored component match
        # silently never indexes this, and the nightly stale sweep HARD DELETES it if an
        # earlier pass had. "This agent's tree", not "any folder called this".
        "ORION/03 Data/SAMPLE/schema-notes.md",
        "LYRA/Content/SAMPLE/brief.md",
        "ARCTURION_Brain/engine/fixtures/SAMPLE/data.json",
        # Mentions in OTHER agents' filenames stay indexed (explicit leg scope).
        "COVENANT/07 🌡 Drift & Anomaly Detection/Ledger/2026-08-04_conformance-THERAPIST.md",
        "ARCTURION_Brain/entities/agents/THERAPIST.md",
        "11 📜 LOGS/1210_MIGRATIONS/inbox-repair-20260720/THERAPIST-inbox.md",
        # Prefix-boundary: component-exact, so a longer name is a different folder.
        "THERAPIST_ARCHIVE/notes.md",
    ]
    for p in anchored_excluded:
        if not excluded_agent_for_path(p):
            failures.append(f"  anchored rule MISSED an owned path: {p}")
        if not should_skip_path(Path(p)):
            failures.append(f"  should_skip_path MISSED an owned path: {p}")
    for p in anchored_kept:
        if excluded_agent_for_path(p):
            failures.append(f"  anchored rule OVER-MATCHED: {p}")

    # ── B-5: the string put in seen_paths must be the one nodes.path holds ───
    # `seen_paths` is the ONLY protection a walked-but-skiplisted node has against the
    # (now nightly) 7c hard delete. On an extract FAILURE there is no node to read
    # `node["path"]` from, so graph_builder derives it with `_rel_to_universe`. If that
    # produced any other spelling, the protection would be a no-op that looks correct.
    from ..extractors.markdown import _rel_to_universe
    from ..extractors import route
    probe = Path(__file__).resolve()
    node, _edges = route(probe, "code", "Module")
    check("seen-path derivation matches the stored node path",
          _rel_to_universe(probe), node["path"])

    # ...and the consequence: a skiplisted node IS protected iff its rel path is present.
    from ..maintenance.content_dedup import gc_stale_skiplisted
    tmp2 = Path(tempfile.mkdtemp(prefix="brain-seen-"))
    orig_db = C.BRAIN_DB
    try:
        C.BRAIN_DB = tmp2 / "b.sqlite"
        from .. import db as DB
        runtime = "ARCTURION_EXTERNAL_RUNTIME/SERVICE_CATALOG.md"   # skiplisted BUT walked
        for seen, expect_removed, label in (({runtime}, 0, "present"), (set(), 1, "absent")):
            c2 = DB.connect(C.BRAIN_DB)
            DB.init_db(c2)
            DB.upsert_node(c2, {
                "id": "mem_rt", "type": "Source", "family": "document",
                "title": "Service Catalog", "path": runtime, "owner": None,
                "status": "active", "governance_tier": 0, "content_hash": "h",
                "summary": "s", "body_excerpt": None, "centrality": 0.0,
                "created": None, "last_modified": "2026-06-16", "last_reviewed": None,
                "metadata": {}})
            c2.commit()
            r = gc_stale_skiplisted(c2, seen, verbose=False,
                                    recovery_log_dir=tmp2 / "logs")
            check(f"extract-failure protection — seen_paths {label}",
                  r["removed"], expect_removed)
            c2.close()
            (tmp2 / "b.sqlite").unlink(missing_ok=True)
    finally:
        C.BRAIN_DB = orig_db

    # ── B-4: recovery log + dry run on the two nightly, unattended passes ────
    from ..maintenance.content_dedup import gc_same_path_twins
    tmp3 = Path(tempfile.mkdtemp(prefix="brain-gclog-"))
    orig_db = C.BRAIN_DB
    try:
        C.BRAIN_DB = tmp3 / "b.sqlite"
        from .. import db as DB
        c3 = DB.connect(C.BRAIN_DB)
        DB.init_db(c3)
        doomed = "ALTAIR/99 🗄️ Archive/Reorg_Backups/old.md"
        base = {"type": "Source", "family": "document", "title": "Old", "owner": None,
                "status": "active", "governance_tier": 0, "content_hash": "h",
                "summary": "s", "body_excerpt": None, "centrality": 0.0,
                "created": None, "last_modified": "2026-06-16", "last_reviewed": None,
                "metadata": {}}
        DB.upsert_node(c3, {**base, "id": "mem_arch", "path": doomed})
        # a same-path twin pair for 7a
        DB.upsert_node(c3, {**base, "id": "mem_twin", "path": "ALTAIR/live.md"})
        DB.upsert_node(c3, {**base, "id": "mem_20260518_twin", "path": "ALTAIR/live.md"})
        c3.commit()

        logs = tmp3 / "logs"
        # DRY RUN writes nothing and deletes nothing, but reports the same blast radius.
        r = gc_stale_skiplisted(c3, set(), verbose=False, apply=False,
                                recovery_log_dir=logs)
        check("7c dry run deletes nothing", r["removed"], 0)
        check("7c dry run reports the candidates", r["candidates"], 1)
        check("7c dry run writes no recovery log", r["recovery_log"], None)
        check("7c dry run left the node alive",
              c3.execute("SELECT 1 FROM nodes WHERE id='mem_arch'").fetchone() is not None,
              True)
        r = gc_same_path_twins(c3, verbose=False, apply=False, recovery_log_dir=logs)
        check("7a dry run deletes nothing", r["losers_removed"], 0)
        check("7a dry run writes no recovery log", r["recovery_log"], None)
        check("7a dry run left both twins alive",
              c3.execute("SELECT COUNT(*) FROM nodes WHERE path='ALTAIR/live.md'"
                         ).fetchone()[0], 2)
        check("dry runs created no log directory at all", logs.exists(), False)

        # APPLIED runs delete AND leave a recovery log naming every id + path.
        import json as _json
        r = gc_stale_skiplisted(c3, set(), verbose=False, recovery_log_dir=logs)
        check("7c applied removes", r["removed"], 1)
        check("7c applied wrote a recovery log", bool(r["recovery_log"]), True)
        rec = _json.loads(Path(r["recovery_log"]).read_text(encoding="utf-8"))
        check("7c log names the id", [x["id"] for x in rec["removed"]], ["mem_arch"])
        check("7c log names the path", rec["removed"][0]["path"], doomed)
        check("7c log records the pass", rec["pass"], "gc_stale_skiplisted")

        r = gc_same_path_twins(c3, verbose=False, recovery_log_dir=logs)
        check("7a applied removes the dated twin", r["losers_removed"], 1)
        rec = _json.loads(Path(r["recovery_log"]).read_text(encoding="utf-8"))
        check("7a log names the loser", [x["id"] for x in rec["removed"]],
              ["mem_20260518_twin"])
        c3.close()
    finally:
        C.BRAIN_DB = orig_db

    # ── B-7: the cap crossing must reach a channel with READERS ─────────────
    # Round 1 printed it to `brain-walker.err` and returned it in the stats dict. The
    # re-review checked both: that .err file has writers and no readers (it holds 272
    # lines of `<frozen>` import noise, so a real signal is buried), and the stats dict's
    # only consumer is `main()`'s print. "Louder than silence" is not the bar. This
    # asserts it lands in the COVENANT inbox — the channel this repo already uses for
    # this failure class — and that the block explains the PRIVACY consequence, not just
    # the truncation, because the reader has to know why it matters at 03:00.
    tmp5 = Path(tempfile.mkdtemp(prefix="brain-inbox-"))
    orig_inbox = C.COVENANT_INBOX
    try:
        C.COVENANT_INBOX = tmp5 / "inbox.md"
        from .. import graph_builder as GB
        GB._alert_cap_reached(150_000, 150_000)
        txt = C.COVENANT_INBOX.read_text(encoding="utf-8")
        check("cap crossing reaches the COVENANT inbox", "CAP REACHED" in txt, True)
        check("...naming the sweep that stopped",
              "gc_stale_skiplisted" in txt, True)
        check("...and the privacy consequence, not just the truncation",
              "EXCLUDED_AGENTS" in txt and "brain_query" in txt, True)
        check("...and the fix", "MAX_FILES_PER_PASS" in txt, True)

        # An APPROACH warning, so a ceiling that took months to reach gives more notice
        # than the crossing itself — and the crossing is what disables the sweep.
        C.COVENANT_INBOX = tmp5 / "approach.md"
        check("no warning well below the threshold",
              GB._warn_cap_approaching(150_000, 41_500), None)
        check("no inbox block written below the threshold",
              C.COVENANT_INBOX.exists(), False)
        msg = GB._warn_cap_approaching(150_000, 125_000)   # 83% > 80%
        check("warns above the threshold", bool(msg and "83%" in msg), True)
        txt2 = C.COVENANT_INBOX.read_text(encoding="utf-8")
        check("...to the inbox, before anything breaks",
              "approaching the file cap" in txt2, True)
        check("...naming the consequence of crossing",
              "EXCLUDED_AGENTS" in txt2, True)
        check("...distinct from the CAP REACHED heading",
              "CAP REACHED" in txt2, False)

        # A privacy sweep that did not run gets its own heading — same consequence as a
        # cap crossing but a different fix, and conflating them sends the reader to the
        # wrong lever.
        C.COVENANT_INBOX = tmp5 / "sweep.md"
        GB._alert_privacy_sweep_skipped("session harvest", "ValueError: boom")
        txt3 = C.COVENANT_INBOX.read_text(encoding="utf-8")
        check("a skipped privacy sweep is alerted",
              "privacy sweep DID NOT RUN" in txt3, True)
        check("...naming what failed and why",
              "session harvest" in txt3 and "ValueError: boom" in txt3, True)

        # An unwritable inbox must not turn a COMPLETED reindex into a crash.
        C.COVENANT_INBOX = tmp5 / "no" / "such" / "dir" / "\0bad"
        GB._alert_cap_reached(1, 1)                       # must not raise
        GB._alert_privacy_sweep_skipped("x", "y")         # must not raise
        GB._warn_cap_approaching(10, 10)                  # must not raise
    finally:
        C.COVENANT_INBOX = orig_inbox

    # ── B-6: the vector cleanup must not swallow a real error ───────────────
    from ..maintenance import content_dedup as CD

    class _Missing:
        def execute(self, *_a, **_k):
            raise sqlite3.OperationalError("no such table: embeddings")

    class _Locked:
        def execute(self, *_a, **_k):
            raise sqlite3.OperationalError("database is locked")

    try:
        CD._drop_derived_rows(_Missing(), "_t", "id")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"  missing table must be tolerated, got {exc!r}")
    raised = False
    try:
        CD._drop_derived_rows(_Locked(), "_t", "id")
    except sqlite3.OperationalError:
        raised = True
    # A swallowed lock silently recreates the orphan-vector class f857f62 fixed.
    check("a locked/failed vector delete PROPAGATES", raised, True)

    if failures:
        print("FAIL — exclusion chokepoint / GC safety:")
        print("\n".join(failures))
        return 1
    print("PASS — exclusion chokepoint: ingest_paths refuses excluded files AND dirs "
          "end-to-end (0 rows, nothing in the skip ledger), control file still indexes, "
          "anchoring keeps ORION/…/SAMPLE/…, seen-path derivation matches nodes.path, "
          "7a/7c dry-run + recovery log, vector-delete errors propagate")
    return 0


if __name__ == "__main__":
    sys.exit(run())
