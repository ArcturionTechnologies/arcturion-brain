"""CLI ingest must not hold the Brain write lock while it scans the VEGA desk.

Regression for 2026-11-07: `python3 -m engine.ingest` called vega_index.auto_sync
inside its own open write transaction, so the 10-90 s workspace scan ran under the
lock and every other Brain writer failed with "database is locked".
Runs against a disposable WAL database only.
"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import config as C
from engine import ingest, vega_corpus, vega_index, walker


class IngestDeskScanLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "VEGA").mkdir()
        self.db = self.root / "brain.sqlite"
        self.pack = self.root / "pack.json"
        self.pack.write_text(json.dumps({
            "version": 1,
            "concepts": [{"id": "trend", "label": "Trend", "kind": "strategy",
                          "area": "strategy", "definition": "Trend", "aliases": []}],
            "predicates": [], "rules": [], "gaps": [],
        }), encoding="utf-8")
        self.source = self.root / "VEGA" / "note.md"
        self.source.write_text("# Note\n", encoding="utf-8")
        with sqlite3.connect(self.db) as setup:
            setup.execute("PRAGMA journal_mode=WAL")
            setup.execute("CREATE TABLE metrics(value TEXT)")

    def _ingest_with_writer_during(self, owner, name, result):
        """Run ingest; a second connection must write while `owner.name` (scan) runs."""
        finished = []

        def during(*args, **kwargs):
            with sqlite3.connect(self.db, timeout=0.5) as other:
                other.row_factory = sqlite3.Row
                other.execute("INSERT INTO metrics VALUES ('other')")
            finished.append(True)
            return result()

        def failing_route(*args, **kwargs):
            # Forces a source-watermark write, so ingest has an open write
            # transaction before the old code reached the desk sync.
            raise ValueError("fixture extractor")

        with patch.object(C, "BRAIN_DB", self.db), \
             patch.object(C, "DESKTOP_ROOT", self.root), \
             patch.object(vega_index, "enabled", return_value=True), \
             patch.object(vega_index, "PACK_PATH", self.pack), \
             patch.object(walker, "walk_paths", return_value=iter([(self.source, "markdown", "note")])), \
             patch.object(ingest, "route", side_effect=failing_route), \
             patch.object(owner, name, side_effect=during):
            stats = ingest.ingest_paths([str(self.source)], embed=False)

        self.assertTrue(finished, "desk scan was not reached")
        self.assertEqual(stats["failures"], 1, "only the fixture extractor should fail; desk sync must succeed")
        with sqlite3.connect(self.db) as check:
            self.assertEqual(check.execute("SELECT count(*) FROM metrics").fetchone()[0], 1)
            # Ingest's own work still committed after the scan moved out.
            self.assertEqual(check.execute(
                "SELECT count(*) FROM source_ingest_watermarks").fetchone()[0], 1)
            self.assertIsNotNone(check.execute(
                "SELECT value FROM vega_desk_meta WHERE key='pack_sha256'").fetchone())

    def test_writer_proceeds_during_desk_declaration_scan(self):
        self._ingest_with_writer_during(vega_index, "_declarations", lambda: ([], []))

    def test_writer_proceeds_during_corpus_scan(self):
        self._ingest_with_writer_during(vega_corpus, "scan", lambda: iter([]))

    def test_desk_apply_failure_keeps_ingest_work_and_counts_failure(self):
        with patch.object(C, "BRAIN_DB", self.db), \
             patch.object(C, "DESKTOP_ROOT", self.root), \
             patch.object(vega_index, "enabled", return_value=True), \
             patch.object(vega_index, "PACK_PATH", self.pack), \
             patch.object(walker, "walk_paths", return_value=iter([(self.source, "markdown", "note")])), \
             patch.object(ingest, "route", side_effect=ValueError("fixture extractor")), \
             patch.object(vega_index, "_apply_prepared_sync", side_effect=RuntimeError("boom")):
            stats = ingest.ingest_paths([str(self.source)], embed=False)
        self.assertEqual(stats["failures"], 2)
        with sqlite3.connect(self.db) as check:
            self.assertEqual(check.execute("SELECT count(*) FROM source_ingest_watermarks").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
