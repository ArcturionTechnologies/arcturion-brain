"""Hermetic tests for live-brain 2026-11-07: historical-copy ranking, verified journals, judge.

Run:  python3 -m engine.tests.test_live_brain_journals
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import brain as B
from engine import journals as J
from engine import judge as JD
from engine import rank_signals as RS


class HistoricalCopy(unittest.TestCase):
    def test_archive_and_backup_shelves(self):
        for p in ("ATLAS/Archive/_config_backups/2026-05-31/x/y.md", "X/99 🗄 Archive/a.md",
                  "X/Backups/2026/a.md", "A/2026-05-31_backup/b.md", "A/_archive/b.md",
                  "A/session_backups/c.md", "A/_superseded/d.md"):
            self.assertTrue(RS.is_historical_copy(p), p)

    def test_live_shelves_and_lookalike_names(self):
        for p in ("ALTAIR/000 🤖 Altair/Memory/Projects/a.md", "X/archive-tools/a.md",
                  "ALTAIR/88 📦 Deliverables/backup-policy-review/a.md", "engine/backup_runner.py",
                  "ATLAS/12. 📈 Trading & Markets HQ/Logs/x.md", "Archive.md", None, ""):
            self.assertFalse(RS.is_historical_copy(p), p)

    def test_archive_markers_untouched(self):
        # The walker skip rules share ARCHIVE_MARKERS; widening it would GC live nodes.
        from engine import config as C
        self.assertNotIn("Archive", C.ARCHIVE_MARKERS)
        self.assertNotIn("_config_backups", C.ARCHIVE_MARKERS)

    def test_authority_demotes_copy_below_live_twin(self):
        live = {"id": "a", "path": "ALTAIR/000 🤖 Altair/Memory/Projects/x.md", "status": "active"}
        copy = {"id": "b", "path": "ATLAS/Archive/_config_backups/x.md", "status": "active"}
        self.assertGreater(B._authority_score(live) - B._authority_score(copy), 19.0)

    def test_status_is_current_intent(self):
        self.assertIn("status", B._RECENCY_TERMS)


class Journals(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src_file = self.root / "proj.md"
        self.src_file.write_text(
            "---\nname: proj\ndescription: \"Desk rebuild; awaiting go\"\nmetadata:\n  modified: 2026-10-23T16:10:36Z\n---\n"
            "Intro line that has no markers at all.\n- **Founder GO 2026-10-21** freeze track. Package DENIED.\n"
            "- OPEN = 13 Founder steps remain in the roles file.\n")
        self.src = {"id": "mem_proj_1", "path": "ALTAIR/000 🤖 Altair/Memory/Projects/proj.md",
                    "title": "proj", "record_type": "project_status", "file": self.src_file}

    def tearDown(self):
        self.tmp.cleanup()

    def test_render_quotes_source_and_pins_hash(self):
        import hashlib
        text, j = J.render(self.src)
        self.assertEqual(j["source_sha256"], hashlib.sha256(self.src_file.read_bytes()).hexdigest())
        self.assertEqual(j["as_of"], "2026-10-23")
        self.assertEqual(j["as_of_from"], "frontmatter:metadata.modified")
        self.assertIn("id: journal:project_status:mem_proj_1", text)
        self.assertIn('evidenced_by: "mem_proj_1"', text)
        # Every state line is verbatim from the source body.
        body = self.src_file.read_text()
        for line in text.splitlines():
            if line.startswith("- L"):
                self.assertIn(line.split(": ", 1)[1], body)
        self.assertNotIn("supersedes:", text)

    def test_verdict_requires_matching_source_and_candidate_bytes(self):
        import hashlib
        sha = hashlib.sha256(self.src_file.read_bytes()).hexdigest()
        yes = {"id": "old1", "path": "ATLAS/Archive/old.md", "content_hash": "h1", "answer": "YES", "reason": "older"}
        att = {"model": JD.MODEL, "effort": JD.EFFORT}
        text, j = J.render(self.src, {sha: {"supersedes": [yes], "attestation": att}, "_candidate_hashes": {"old1": "h1"}})
        self.assertEqual(j["supersedes"], ["old1"])
        self.assertIn('supersedes:\n  - "old1"', text)
        # Candidate changed since the verdict -> verdict no longer applies.
        _, j2 = J.render(self.src, {sha: {"supersedes": [yes], "attestation": att}, "_candidate_hashes": {"old1": "h2"}})
        self.assertEqual(j2["supersedes"], [])
        # Source changed since the verdict -> verdict no longer applies.
        _, j3 = J.render(self.src, {"other-sha": {"supersedes": [yes], "attestation": att}, "_candidate_hashes": {"old1": "h1"}})
        self.assertEqual(j3["supersedes"], [])

    def test_journal_source_path_only_for_journal_nodes(self):
        meta = {"journal": {"source_path": "A/b.md"}}
        self.assertEqual(RS.journal_source_path({"id": "journal:decision:x", "metadata": json.dumps(meta)}), "A/b.md")
        self.assertIsNone(RS.journal_source_path({"id": "mem_x", "metadata": meta}))

    def test_stem_key(self):
        self.assertEqual(J.stem_key("a/2026-05-27-vega-refresh.md"), "vega-refresh")
        self.assertEqual(J.stem_key("a/storage-audit-2026-11-05.md"), "storage-audit")


class Judge(unittest.TestCase):
    def test_unattested_run_is_rejected(self):
        def fake_run(cmd, **kw):
            self.assertEqual(kw.get("stdin"), subprocess.DEVNULL)
            self.assertIn("gpt-6.1-terra", cmd)
            self.assertIn('model_reasoning_effort="high"', cmd)
            out = Path(cmd[cmd.index("-o") + 1])
            out.write_text(json.dumps({"verdicts": []}))
            return subprocess.CompletedProcess(cmd, 0, stdout='{"type":"thread.started","thread_id":"nope-0000"}\n', stderr="")
        with mock.patch.object(JD.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(JD, "rollout_attestation", return_value=None):
            with self.assertRaises(RuntimeError):
                JD.ask("prompt")

    def test_batch_key_changes_with_bytes(self):
        j = {"source_sha256": "s1"}
        c = [{"id": "x", "content_hash": "h1"}]
        self.assertNotEqual(JD.batch_key(j, c), JD.batch_key({"source_sha256": "s2"}, c))
        self.assertNotEqual(JD.batch_key(j, c), JD.batch_key(j, [{"id": "x", "content_hash": "h2"}]))

    def test_failure_records_no_verdict(self):
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(JD, "STATE_DIR", Path(d)), \
             mock.patch.object(JD, "ask", side_effect=subprocess.TimeoutExpired("codex", 1)):
            res = JD.judge_batches([{"journal": {"id": "journal:decision:a", "record_type": "decision",
                                                 "as_of": "2026-11-01", "source_sha256": "s"},
                                     "current_text": "t", "candidates": [{"id": "x", "path": "p", "content_hash": "h"}]}],
                                   workers=1)
            self.assertEqual(res["failed"], 1)
            data = json.loads((Path(d) / "supersession_cache.json").read_text())
            self.assertEqual(data["batches"], {})
            self.assertEqual(len(data["failures"]), 1)


class AnswerResolution(unittest.TestCase):
    def setUp(self):
        import sqlite3
        from engine import evidence_fields as EF
        self.EF = EF
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE nodes(id TEXT PRIMARY KEY, content_hash TEXT)")
        self.conn.execute("CREATE TABLE edges(from_id TEXT, to_id TEXT, predicate TEXT)")
        self.conn.execute("INSERT INTO nodes VALUES('src','S1')")
        self.conn.execute("INSERT INTO edges VALUES('src','journal:decision:src','supports')")
        meta = {"journal": {"source_node": "src", "source_sha256": "S1", "source_path": "A/x.md"}}
        self.j = {"id": "journal:decision:src", "content_hash": "J1", "metadata": json.dumps(meta),
                  "title": "Decision record", "summary": "s", "body_excerpt": "b"}
        self.other = {"id": "mem_other", "content_hash": "O1"}
        self.p = [mock.patch.object(EF, "CACHE_PATH", Path(self.tmp.name) / "v.json"),
                  mock.patch.object(EF, "STATE_DIR", Path(self.tmp.name)),
                  mock.patch.object(EF, "ASYNC", False)]
        for x in self.p:
            x.start()
        EF._CACHE.update({"mtime": None, "data": None})

    def tearDown(self):
        for x in self.p:
            x.stop()
        self.EF._CACHE.update({"mtime": None, "data": None})
        self.tmp.cleanup()

    def _verdict(self, answer):
        key = JD.answer_key("q?", self.j["id"], "J1")
        (Path(self.tmp.name) / "v.json").write_text(json.dumps(
            {"verdicts": {key: {"answer": answer}}, "failures": [], "calls_by_day": {}}))
        self.EF._CACHE.update({"mtime": None, "data": None})

    def test_no_verdict_is_unchanged(self):
        nodes = [self.other, self.j]
        self.assertEqual(self.EF.resolve("q?", nodes, self.conn, False), (nodes, False))

    def test_yes_leads_with_journal_and_admits(self):
        self._verdict("YES")
        nodes, ok = self.EF.resolve("q?", [self.other, self.j], self.conn, False)
        self.assertTrue(ok)
        self.assertEqual(nodes[0]["id"], self.j["id"])

    def test_no_verdict_keeps_order(self):
        self._verdict("NO")
        nodes, ok = self.EF.resolve("q?", [self.other, self.j], self.conn, True)
        self.assertEqual([n["id"] for n in nodes], ["mem_other", self.j["id"]])

    def test_yes_never_jumps_a_newer_current_record(self):
        self._verdict("YES")
        meta = json.loads(self.j["metadata"]); meta["journal"]["as_of"] = "2026-08-07"
        self.j["metadata"] = json.dumps(meta)
        newer = {"id": "mem_new", "path": "ALTAIR/000 🤖 Altair/Memory/Projects/new.md", "last_modified": "2026-11-06T00:00"}
        nodes, ok = self.EF.resolve("q?", [newer, self.j], self.conn, True)
        self.assertEqual([n["id"] for n in nodes], ["mem_new", self.j["id"]])
        self.assertTrue(ok)

    def test_unverified_journal_ignored(self):
        self._verdict("YES")
        self.conn.execute("UPDATE nodes SET content_hash='CHANGED' WHERE id='src'")
        nodes, ok = self.EF.resolve("q?", [self.other, self.j], self.conn, False)
        self.assertFalse(ok)
        self.assertEqual(nodes[0]["id"], "mem_other")


class IngestConnectionGuard(unittest.TestCase):
    def test_failed_init_closes_write_connection(self):
        import sqlite3
        from engine import ingest as I
        made = []

        def fake_connect(*a, **k):
            c = sqlite3.connect(":memory:")
            c.execute("CREATE TABLE t(x)")
            c.execute("INSERT INTO t VALUES (1)")  # open write transaction
            made.append(c)
            return c

        def boom(conn):
            raise sqlite3.OperationalError("database is locked")
        with mock.patch.object(I.DB, "connect", side_effect=fake_connect), \
             mock.patch.object(I.DB, "init_db", side_effect=boom), \
             mock.patch.object(I.walker, "walk_paths", return_value=iter([])):
            with self.assertRaises(sqlite3.OperationalError):
                I.ingest_paths([], embed=False)
        self.assertEqual(len(made), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            made[0].execute("SELECT 1")  # closed


class WatcherCoalescing(unittest.TestCase):
    def test_idle_only_after_pipe_drained(self):
        import os
        from engine import watch as W
        r, w = os.pipe()
        os.write(w, b"a\0__M__\0b\0__M__\0")
        gen = W._nul_tokens(os.fdopen(r, "rb", buffering=0))
        got = [next(gen) for _ in range(5)]
        self.assertEqual(got[:4], ["a", "__M__", "b", "__M__"])
        self.assertIs(got[4], W.IDLE)  # two queued batches, one idle point -> one flush
        os.close(w)


if __name__ == "__main__":
    unittest.main(verbosity=2)
