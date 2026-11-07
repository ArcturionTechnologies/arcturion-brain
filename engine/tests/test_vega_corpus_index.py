import json
import hashlib
import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from engine import vega_corpus, vega_corpus_index, vega_index, watch, db


class CorpusIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.shelf = self.root / "VEGA/02 📈 Strategies & Playbooks"
        self.shelf.mkdir(parents=True)
        self.pack_path = self.root / "pack.json"
        self.pack = {"version": 1, "concepts": [
            {"id": "trend", "label": "Trend", "kind": "strategy", "area": "strategy", "definition": "A trend strategy", "aliases": []},
            {"id": "breakout", "label": "Breakout", "kind": "condition", "area": "strategy", "definition": "A breakout", "aliases": []},
        ], "predicates": [{"id": "vega_requires", "label": "requires", "from_kinds": ["strategy"], "to_kinds": ["condition"]}], "rules": [], "gaps": []}
        self.pack_path.write_text(json.dumps(self.pack), encoding="utf-8")
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE nodes(id TEXT PRIMARY KEY,path TEXT,content_hash TEXT)")

    def source(self, name, text):
        path = self.shelf / name
        path.write_text(text, encoding="utf-8")
        rel = path.relative_to(self.root).as_posix()
        self.conn.execute("INSERT INTO nodes(id,path) VALUES (?,?)", (rel, rel))
        return path, rel

    def sync(self, paths=None):
        return vega_corpus_index.sync_corpus(self.conn, root=self.root, pack_path=self.pack_path, paths=paths)

    def test_full_and_incremental_preserve_independent_evidence(self):
        first, ar = self.source("one.md", "# First\nTrend requires Breakout.\n")
        _, br = self.source("two.md", "# Second\nTrend uses Breakout.\n")
        vega_index.sync(self.conn, root=self.root, pack_path=self.pack_path)
        result = self.sync()
        self.assertEqual(sum(r["count"] for r in result["inventory"] if r["disposition"] == "eligible"), 2)
        self.assertGreater(self.conn.execute("SELECT count(*) FROM vega_desk_claim_origin WHERE origin='corpus'").fetchone()[0], 2)
        before = self.conn.execute("SELECT count(*) FROM vega_desk_edges e JOIN vega_desk_evidence v ON v.id=e.evidence_id WHERE v.source_path=?", (br,)).fetchone()[0]
        first.unlink()
        self.sync([first])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vega_desk_edges e JOIN vega_desk_evidence v ON v.id=e.evidence_id JOIN vega_desk_claim_origin o ON o.edge_id=e.id WHERE v.source_path=? AND o.origin='corpus'", (ar,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vega_desk_edges e JOIN vega_desk_evidence v ON v.id=e.evidence_id WHERE v.source_path=?", (br,)).fetchone()[0], before)
        # A direct seed/declaration sync cannot erase unrelated corpus rows.
        vega_index.sync(self.conn, root=self.root, pack_path=self.pack_path)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vega_desk_edges e JOIN vega_desk_evidence v ON v.id=e.evidence_id WHERE v.source_path=?", (br,)).fetchone()[0], before)

    def test_excluded_memory_body_is_not_opened_and_path_is_hashed(self):
        self.source("eligible.md", "# Trend\nTrend requires Breakout.\n")
        private = self.shelf / "Memory"
        private.mkdir()
        (private / "private-name.md").write_text("private sentinel", encoding="utf-8")
        real_open = vega_corpus.os.open
        def guarded(path, flags, *args):
            if "Memory" in str(path):
                raise AssertionError("Memory body was opened")
            return real_open(path, flags, *args)
        with patch.object(vega_corpus.os, "open", side_effect=guarded):
            result = self.sync()
        self.assertTrue(any(r["disposition"] == "excluded" for r in result["inventory"]))
        all_rows = repr(self.conn.execute("SELECT * FROM vega_desk_inventory").fetchall())
        self.assertNotIn("Memory", all_rows)
        self.assertNotIn("private-name", all_rows)

    def test_unresolved_source_is_reported_and_never_accepted(self):
        path = self.shelf / "unindexed.md"
        path.write_text("# Strategy\nTrend requires Breakout.\n", encoding="utf-8")
        result = self.sync()
        row = next(r for r in result["inventory"] if r["disposition"] == "eligible")
        self.assertEqual(row["canonical_attached"], 0)
        self.assertGreater(row["documents_queued"], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vega_desk_edges WHERE status='accepted'").fetchone()[0], 0)

    def test_malformed_or_excluded_payload_fails_atomically(self):
        self.source("source.md", "# Trend\nTrend requires Breakout.\n")
        self.sync()
        before = self.conn.execute("SELECT count(*) FROM vega_desk_edges").fetchone()[0]
        bad = [{"source": "VEGA/Memory/private.md", "area": "other", "disposition": "excluded", "reason": "protected_file", "source_hash": "a" * 64, "concepts": [], "rules": []}]
        with patch.object(vega_corpus, "scan", return_value=iter(bad)):
            with self.assertRaises(vega_index.PackError):
                self.sync()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vega_desk_edges").fetchone()[0], before)

    def test_directory_replaced_with_symlink_removes_descendant_inventory(self):
        directory = self.shelf / "folder"
        directory.mkdir()
        path = directory / "document.md"
        path.write_text("# Trend\nTrend requires Breakout.\n", encoding="utf-8")
        rel = path.relative_to(self.root).as_posix()
        self.conn.execute("INSERT INTO nodes(id,path) VALUES (?,?)", (rel, rel))
        self.sync()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vega_desk_inventory WHERE disposition='eligible'").fetchone()[0], 1)
        directory.rename(self.root / "moved")
        directory.symlink_to(self.root / "moved", target_is_directory=True)
        self.sync([directory])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vega_desk_inventory WHERE disposition='eligible'").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vega_desk_edges WHERE status='accepted'").fetchone()[0], 0)

    def test_watcher_routes_excluded_events_without_global_body_ingest(self):
        eligible, _ = self.source("eligible.md", "# Trend\n")
        protected = self.shelf / "Positions" / "private.md"
        protected.parent.mkdir()
        protected.write_text("private", encoding="utf-8")
        fake_conn = MagicMock()
        with patch.object(watch.C, "DESKTOP_ROOT", self.root), patch.object(vega_index, "enabled", return_value=True), patch.object(watch, "ingest_paths") as ingest, patch.object(db, "connect", return_value=fake_conn), patch.object(vega_index, "auto_sync") as automatic, patch.object(watch, "log"):
            self.assertTrue(watch.watch_event_allowed(str(protected), []))
            watch.flush({str(protected), str(eligible)})
            self.assertEqual(ingest.call_args.args[0], [str(eligible)])
            self.assertEqual(automatic.call_args.kwargs["paths"], [str(protected)])

    def test_declaration_scanner_never_opens_excluded_vega_bodies(self):
        knowledge = self.root / "VEGA/000 📈 VEGA/Knowledge"
        for name in ("Positions", "Orders", "raw", "Memory"):
            directory = knowledge / name
            directory.mkdir(parents=True)
            (directory / "private.md").write_text("Signal confirms Risk.", encoding="utf-8")
        original = Path.read_text
        original_iterdir = Path.iterdir
        def guarded(path, *args, **kwargs):
            if any(name in path.parts for name in ("Positions", "Orders", "raw", "Memory")):
                raise AssertionError("excluded body was read")
            return original(path, *args, **kwargs)
        def guarded_iterdir(path):
            if any(name in path.parts for name in ("Positions", "Orders", "raw", "Memory")):
                raise AssertionError("excluded subtree was traversed")
            return original_iterdir(path)
        with patch.object(Path, "read_text", guarded), patch.object(Path, "iterdir", guarded_iterdir):
            vega_index.sync(self.conn, root=self.root, pack_path=self.pack_path)

    def test_existing_memory_watcher_behavior_is_preserved(self):
        memory = self.root / "VEGA/000 📈 VEGA/Memory"
        memory.mkdir(parents=True)
        path = memory / "saved.md"
        path.write_text("private", encoding="utf-8")
        with patch.object(watch.C, "DESKTOP_ROOT", self.root), patch.object(watch.C, "BRAIN_ROOT", self.root / "no-brain"), patch.object(watch.C, "CONSTELLATION_AGENTS", ["VEGA"]), patch.object(vega_index, "enabled", return_value=True), patch.object(watch, "ingest_paths") as ingest, patch.object(watch, "log"):
            watch.flush({str(path)})
            self.assertEqual(ingest.call_args.args[0], [str(path)])

    def test_pinned_claim_requires_exact_source_hash_and_rule(self):
        path, rel = self.source("pinned.md", "Trend requires Breakout.\n")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        claim = {"source": rel, "quote": "Trend requires Breakout.", "from": "trend", "predicate": "vega_corpus_requires", "to": "breakout", "kind": "definition", "context": {"scope": "historical"}}
        pin = {**claim, "source_hash": digest}
        record = {"source": rel, "area": "strategies", "disposition": "eligible", "reason": "authored_markdown", "source_hash": digest, "concepts": [], "rules": [claim]}
        with patch.object(vega_corpus, "verified_pinned_rules", return_value=[pin]), patch.object(vega_corpus, "scan", return_value=iter([record])):
            self.sync()
        self.assertEqual(self.conn.execute("SELECT status FROM vega_desk_edges").fetchone()[0], "accepted")
        path.write_text("Trend requires Breakout.\nAdditional context.\n", encoding="utf-8")
        changed = {**record, "source_hash": hashlib.sha256(path.read_bytes()).hexdigest()}
        with patch.object(vega_corpus, "verified_pinned_rules", return_value=[pin]), patch.object(vega_corpus, "scan", return_value=iter([changed])):
            self.sync([path])
        self.assertEqual(self.conn.execute("SELECT status FROM vega_desk_edges").fetchone()[0], "queued")

    def test_operator_sync_and_coverage_use_explicit_disposable_index(self):
        path = self.shelf / "operator.md"
        path.write_text("# Trend\nTrend requires Breakout.\n", encoding="utf-8")
        rel = path.relative_to(self.root).as_posix()
        database = self.root / "fixture.sqlite"
        connection = sqlite3.connect(database)
        connection.execute("CREATE TABLE nodes(id TEXT PRIMARY KEY,path TEXT,content_hash TEXT)")
        connection.execute("INSERT INTO nodes(id,path) VALUES (?,?)", (rel, rel))
        connection.commit()
        connection.close()
        prefix = ["--db", str(database), "--root", str(self.root), "--pack", str(self.pack_path)]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            vega_index.main([*prefix, "sync"])
        self.assertIn("corpus", json.loads(output.getvalue()))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            vega_index.main([*prefix, "coverage"])
        summary = json.loads(output.getvalue())
        self.assertTrue(summary["inventory"])
        self.assertIsNone(summary["query_visible"])  # fixture lacks trusted admission metadata
        connection = sqlite3.connect(database)
        self.assertEqual(connection.execute("SELECT count(*) FROM nodes").fetchone()[0], 1)
        connection.close()


if __name__ == "__main__":
    unittest.main()
