"""SQLite writer contention checks against disposable WAL databases only."""
import contextlib
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import vega_corpus, vega_corpus_index, vega_index


class DeskWriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "VEGA").mkdir()
        self.db = self.root / "test.sqlite"
        self.pack = self.root / "pack.json"
        self.pack.write_text(json.dumps({
            "version": 1,
            "concepts": [{"id": "trend", "label": "Trend", "kind": "strategy",
                          "area": "strategy", "definition": "Trend", "aliases": []}],
            "predicates": [], "rules": [], "gaps": [],
        }), encoding="utf-8")
        self.conn = sqlite3.connect(self.db, timeout=3)
        self.addCleanup(self.conn.close)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("CREATE TABLE nodes(id TEXT PRIMARY KEY, path TEXT, content_hash TEXT)")
        self.conn.execute("CREATE TABLE metrics(value TEXT)")
        self.conn.commit()

    def writer_during(self, action, callback_owner, callback_name, callback_result):
        attempted, completed = threading.Event(), threading.Event()
        failures = []

        def other_writer():
            try:
                with sqlite3.connect(self.db, timeout=3) as other:
                    attempted.set()
                    other.execute("INSERT INTO metrics VALUES ('other')")
            except Exception as exc:
                failures.append(exc)
            finally:
                completed.set()

        thread = threading.Thread(target=other_writer)

        def during(*args, **kwargs):
            thread.start()
            self.assertTrue(attempted.wait(1))
            self.assertFalse(completed.wait(0.15), "competing writer bypassed the reservation")
            return callback_result()

        try:
            with patch.object(callback_owner, callback_name, side_effect=during):
                action()
            if self.conn.in_transaction:
                self.assertFalse(completed.is_set())
                self.conn.commit()
        finally:
            if self.conn.in_transaction:
                self.conn.rollback()
            if thread.ident is not None:
                thread.join(4)
        self.assertTrue(completed.is_set())
        self.assertEqual(failures, [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM metrics").fetchone()[0], 1)

    def writer_proceeds_during(self, action, callback_owner, callback_name, callback_result):
        """A competing writer must finish while the slow workspace scan runs."""
        finished = []

        def during(*args, **kwargs):
            with sqlite3.connect(self.db, timeout=0.5) as other:
                other.execute("INSERT INTO metrics VALUES ('other')")
            finished.append(True)
            return callback_result()

        with patch.object(callback_owner, callback_name, side_effect=during):
            action()
        if self.conn.in_transaction:
            self.conn.commit()
        self.assertTrue(finished, "scan callback was not reached")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM metrics").fetchone()[0], len(finished))

    def test_auto_sync_scans_before_reserving_writer(self):
        with patch.object(vega_index, "enabled", return_value=True), \
             patch.object(vega_index, "PACK_PATH", self.pack), \
             patch.object(vega_index.C, "DESKTOP_ROOT", self.root):
            self.writer_proceeds_during(lambda: vega_index.auto_sync(self.conn),
                                        vega_index, "_declarations", lambda: ([], []))
            self.conn.execute("DELETE FROM metrics")
            self.conn.commit()
            self.writer_proceeds_during(lambda: vega_index.auto_sync(self.conn),
                                        vega_corpus, "scan", lambda: iter([]))

    def test_standalone_corpus_sync_scans_before_reserving_writer(self):
        self.writer_proceeds_during(
            lambda: vega_corpus_index.sync_corpus(self.conn, root=self.root, pack_path=self.pack),
            vega_corpus, "scan", lambda: iter([]))

    def test_auto_sync_with_row_factory_connection(self):
        # The live watcher connects with row_factory=sqlite3.Row.
        self.conn.row_factory = sqlite3.Row
        with patch.object(vega_index, "enabled", return_value=True), \
             patch.object(vega_index, "PACK_PATH", self.pack), \
             patch.object(vega_index.C, "DESKTOP_ROOT", self.root):
            vega_index.auto_sync(self.conn)
            self.conn.commit()
            result = vega_index.auto_sync(self.conn, paths=[self.root / "VEGA/unrelated.md"])
            self.conn.commit()
        self.assertIn("inventory", result["corpus"])

    def test_plan_inputs_changed_during_scan_are_rechecked_under_lock(self):
        vega_index.sync(self.conn, root=self.root, pack_path=self.pack)
        self.conn.commit()
        real = self.conn.execute("SELECT value FROM vega_desk_meta WHERE key='pack_sha256'").fetchone()[0]

        def tamper(*args, **kwargs):
            # Another writer invalidates the stored pack hash mid-scan; the
            # incremental "unchanged" plan must not be applied blindly.
            with sqlite3.connect(self.db, timeout=0.5) as other:
                other.execute("UPDATE vega_desk_meta SET value='stale' WHERE key='pack_sha256'")
            return [], []

        with patch.object(vega_index, "_declarations", side_effect=tamper):
            first = vega_index._prepare_sync(self.conn, root=self.root, pack_path=self.pack,
                                             paths=[self.root / "VEGA/unrelated.md"])
        self.assertIn("result", first)  # planned as a no-op before the tamper landed
        self.conn.execute("BEGIN IMMEDIATE")
        result = vega_index._apply_prepared_sync(self.conn, first, {"root": self.root, "pack_path": self.pack,
                                                                    "paths": [self.root / "VEGA/unrelated.md"]})
        self.conn.commit()
        self.assertNotIn("unchanged", result)
        self.assertEqual(self.conn.execute("SELECT value FROM vega_desk_meta WHERE key='pack_sha256'").fetchone()[0], real)

    def test_operator_sync_reserves_before_metadata_read(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.writer_during(
                lambda: vega_index.main(["--db", str(self.db), "--root", str(self.root),
                                         "--pack", str(self.pack), "sync"]),
                vega_index, "_declarations", lambda: ([], []))

    def test_caller_transaction_survives_failed_auto_sync(self):
        self.conn.execute("INSERT INTO metrics VALUES ('caller')")
        with patch.object(vega_index, "enabled", return_value=True), \
             patch.object(vega_index, "PACK_PATH", self.pack), \
             patch.object(vega_index.C, "DESKTOP_ROOT", self.root), \
             patch.object(vega_corpus_index, "apply_prepared_corpus", side_effect=ValueError("failed")):
            with self.assertRaisesRegex(ValueError, "failed"):
                vega_index.auto_sync(self.conn)
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT value FROM metrics").fetchall(), [("caller",)])
        self.conn.rollback()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM metrics").fetchone()[0], 0)

    def test_owned_failure_and_noop_release_reservation(self):
        with self.assertRaises(vega_index.PackError):
            vega_index.sync(self.conn, root=self.root, pack_path=self.root / "missing")
        self.assertFalse(self.conn.in_transaction)
        vega_index.sync(self.conn, root=self.root, pack_path=self.pack)
        self.conn.commit()
        result = vega_index.sync(self.conn, root=self.root, pack_path=self.pack,
                                 paths=[self.root / "unrelated.md"])
        self.assertTrue(result["unchanged"])
        self.assertFalse(self.conn.in_transaction)
        result = vega_corpus_index.sync_corpus(self.conn, root=self.root, pack_path=self.pack,
                                              paths=[self.root / "ATLAS/unrelated.md"])
        self.assertTrue(result["unchanged"])
        self.assertFalse(self.conn.in_transaction)


if __name__ == "__main__":
    unittest.main()
