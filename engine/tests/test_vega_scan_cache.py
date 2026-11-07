"""Stat-keyed scan cache and excluded-directory selection (desk-scan cost fix, 2026-11-07)."""
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import vega_corpus, vega_corpus_index, vega_index

PACK = {"version": 1, "concepts": [
    {"id": "trend", "label": "Trend", "kind": "strategy", "area": "strategy", "definition": "A trend strategy", "aliases": []},
    {"id": "breakout", "label": "Breakout", "kind": "condition", "area": "strategy", "definition": "A breakout", "aliases": []},
], "predicates": [{"id": "vega_requires", "label": "requires", "from_kinds": ["strategy"], "to_kinds": ["condition"]}], "rules": [], "gaps": []}


class ScanCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.shelf = self.root / "VEGA/02 📈 Strategies & Playbooks"
        self.shelf.mkdir(parents=True)
        vega_corpus.clear_record_cache()
        self.addCleanup(vega_corpus.clear_record_cache)
        # Files written by a test are "racy" (just changed); let them be cached.
        patcher = patch.object(vega_corpus, "_RACY_NS", -10**18)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.opened = []
        real_open = os.open

        def counting_open(path, *args, **kwargs):
            self.opened.append(Path(path).name)
            return real_open(path, *args, **kwargs)
        patcher = patch.object(vega_corpus.os, "open", counting_open)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, name, text):
        path = self.shelf / name
        path.write_text(text, encoding="utf-8")
        return path

    def scan(self, paths=None, pack=PACK, rule_config=None):
        return {r["source"]: r for r in vega_corpus.scan(self.root, paths=paths, pack=pack, rule_config=rule_config)}

    def test_unchanged_file_is_served_from_cache_with_identical_record(self):
        self.write("one.md", "# One\nTrend requires Breakout.\n")
        first = self.scan()
        self.assertEqual(self.opened, ["one.md"])
        second = self.scan()
        self.assertEqual(self.opened, ["one.md"], "unchanged file was re-read")
        self.assertEqual(first, second)

    def test_changed_content_is_reread_even_at_same_size(self):
        path = self.write("one.md", "# One\nTrend requires Breakout.\n")
        before = self.scan()
        path.write_text("# Two\nTrend requires Breakout.\n", encoding="utf-8")  # same length
        after = self.scan()
        self.assertEqual(self.opened, ["one.md", "one.md"])
        rel = next(iter(after))
        self.assertNotEqual(before[rel]["source_hash"], after[rel]["source_hash"])

    def test_recently_changed_file_is_not_cached(self):
        self.write("one.md", "# One\nTrend requires Breakout.\n")
        with patch.object(vega_corpus, "_RACY_NS", 10**15):
            self.scan()
            self.scan()
        self.assertEqual(self.opened, ["one.md", "one.md"])

    def test_pack_change_invalidates_cache(self):
        self.write("one.md", "# One\nTrend requires Breakout.\n")
        self.scan()
        other = json.loads(json.dumps(PACK))
        other["concepts"][1]["aliases"] = ["Range break"]
        self.scan(pack=other)
        self.assertEqual(self.opened, ["one.md", "one.md"])

    def test_pinned_rules_change_invalidates_cache(self):
        self.write("one.md", "Invented evidence sentence.")
        self.scan()
        self.scan(rule_config={"grounded": [], "pins": []})
        self.assertEqual(self.opened, ["one.md", "one.md"])

    def test_caller_mutation_cannot_poison_cache(self):
        self.write("one.md", "# One\nTrend requires Breakout.\n")
        first = self.scan()
        rec = next(iter(first.values()))
        rec["rules"].clear()
        rec["concepts"].append({"id": "doc_bogus"})
        again = next(iter(self.scan().values()))
        self.assertTrue(again["rules"])
        self.assertNotIn({"id": "doc_bogus"}, again["concepts"])

    def test_duplicates_are_recomputed_per_pass(self):
        self.write("a.md", "# Same\nTrend requires Breakout.\n")
        self.write("b.md", "# Same\nTrend requires Breakout.\n")
        first = self.scan()
        second = self.scan()
        self.assertEqual([r["duplicate_of"] for r in first.values()], [r["duplicate_of"] for r in second.values()])
        self.assertEqual(sum(1 for r in second.values() if r["duplicate_of"]), 1)

    def test_full_scan_drops_deleted_entries(self):
        gone = self.write("gone.md", "# Gone\nTrend requires Breakout.\n")
        self.write("kept.md", "# Kept\nTrend requires Breakout.\n")
        self.scan()
        self.assertEqual(len(vega_corpus._RECORD_CACHE), 2)
        gone.unlink()
        self.scan()
        self.assertEqual([k[1].rsplit("/", 1)[-1] for k in vega_corpus._RECORD_CACHE], ["kept.md"])


class ExcludedDirectorySelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.desk = self.root / "VEGA/03 🤖 Execution & Paper Trading/desk-roles"
        (self.desk / "state").mkdir(parents=True)
        (self.desk / "notes").mkdir()
        (self.desk / "alerts").mkdir()
        self.shelf = self.root / "VEGA/02 📈 Strategies & Playbooks"
        self.shelf.mkdir(parents=True)
        (self.shelf / "one.md").write_text("# One\nTrend requires Breakout.\n", encoding="utf-8")
        self.pack_path = self.root / "pack.json"
        self.pack_path.write_text(json.dumps(PACK), encoding="utf-8")
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row  # as db.connect() / the live watcher
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE nodes(id TEXT PRIMARY KEY,path TEXT,content_hash TEXT)")
        vega_corpus.clear_record_cache()
        self.addCleanup(vega_corpus.clear_record_cache)
        vega_index.sync(self.conn, root=self.root, pack_path=self.pack_path)
        vega_corpus_index.sync_corpus(self.conn, root=self.root, pack_path=self.pack_path)
        self.conn.commit()

    def rel(self, path):
        return path.relative_to(self.root).as_posix()

    def prepare(self, *paths):
        return vega_corpus_index.prepare_corpus(self.conn, paths=[self.rel(p) for p in paths], root=self.root, pack_path=self.pack_path)

    def test_excluded_directory_classification(self):
        self.assertTrue(vega_corpus.excluded_directory(self.rel(self.desk / "state")))
        self.assertTrue(vega_corpus.excluded_directory(self.rel(self.desk / "state/coordination")))
        self.assertTrue(vega_corpus.excluded_directory(self.rel(self.desk / "notes")))
        self.assertFalse(vega_corpus.excluded_directory(self.rel(self.desk / "alerts")))
        self.assertFalse(vega_corpus.excluded_directory(self.rel(self.shelf)))
        self.assertFalse(vega_corpus.excluded_directory("ATLAS/x"))

    def test_runtime_state_directory_event_stays_incremental(self):
        (self.desk / "state/lean-health.json").write_text("{}", encoding="utf-8")
        plan = self.prepare(self.desk / "state", self.desk / "state/lean-health.json")
        self.assertIsNotNone(plan["selected"])
        self.assertEqual({r["disposition"] for r in plan["source_records"].values()}, {"excluded"})
        self.assertEqual(set(plan["source_records"]), {self.rel(self.desk / "state"), self.rel(self.desk / "state/lean-health.json")})

    def test_excluded_directory_record_matches_full_scan(self):
        source = self.rel(self.desk / "state")
        full = {r["source"]: r for r in vega_corpus.scan(self.root, pack=PACK)}
        [part] = list(vega_corpus.scan(self.root, paths=[source], pack=PACK))
        self.assertEqual(part, full[source])

    def test_admitted_directory_event_still_reconciles_fully(self):
        self.assertIsNone(self.prepare(self.desk / "alerts")["selected"])
        self.assertIsNone(self.prepare(self.shelf)["selected"])

    def test_symlinked_excluded_directory_still_reconciles_fully(self):
        target = self.root / "elsewhere"
        target.mkdir()
        link = self.desk / "state/linked"
        link.symlink_to(target, target_is_directory=True)
        self.assertIsNone(self.prepare(link)["selected"])

    def test_auto_sync_state_event_applies_with_row_connection(self):
        with patch.object(vega_index, "enabled", return_value=True), \
                patch.object(vega_index.C, "DESKTOP_ROOT", self.root), \
                patch.object(vega_index, "PACK_PATH", self.pack_path), \
                patch.object(vega_corpus_index.C, "DESKTOP_ROOT", self.root):
            result = vega_index.auto_sync(self.conn, paths=[str(self.desk / "state")])
        self.conn.commit()
        self.assertTrue(result["desk"].get("unchanged"))
        inventory = self.conn.execute("SELECT disposition,reason FROM vega_desk_inventory WHERE path_hash=?", (vega_index._hash(self.rel(self.desk / "state")),)).fetchone()
        self.assertEqual(tuple(inventory), ("excluded", "pruned_subtree"))
        eligible = self.conn.execute("SELECT count(*) FROM vega_desk_inventory WHERE disposition='eligible'").fetchone()[0]
        self.assertEqual(eligible, 1)


if __name__ == "__main__":
    unittest.main()
