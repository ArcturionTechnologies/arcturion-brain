import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import vega_index as v
from engine import watch
from engine import ontology, validator


class DeskIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.shelf = self.root / v.DECLARATION_ROOTS[0]
        self.shelf.mkdir(parents=True)
        self.pack_path = self.root / "pack.json"
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.execute("CREATE TABLE nodes(id TEXT PRIMARY KEY,path TEXT,content_hash TEXT)")
        self.pack = {
            "version": 1,
            "concepts": [
                {"id": "signal", "label": "Signal", "kind": "signal", "area": "setups", "definition": "A signal", "aliases": []},
                {"id": "risk", "label": "Risk", "kind": "factor", "area": "risk", "definition": "A risk", "aliases": []},
                {"id": "risk2", "label": "Risk 2", "kind": "factor", "area": "risk", "definition": "A risk", "aliases": []},
            ],
            "predicates": [{"id": "vega_confirms", "label": "confirms", "from_kinds": ["signal"], "to_kinds": ["factor"]}],
            "rules": [], "gaps": [],
        }

    def source(self, name, content):
        path = self.shelf / name
        path.write_text(content, encoding="utf-8")
        rel = path.relative_to(self.root).as_posix()
        self.conn.execute("INSERT OR REPLACE INTO nodes(id,path) VALUES (?,?)", (rel, rel))
        return path, rel

    def rule(self, source, quote, **extra):
        return {"source": source, "quote": quote, "from": "signal", "to": "risk", "predicate": "vega_confirms", "kind": "doctrine", **extra}

    def sync(self, paths=None):
        self.pack_path.write_text(json.dumps(self.pack), encoding="utf-8")
        return v.sync(self.conn, paths=paths, root=self.root, pack_path=self.pack_path)

    def fingerprint(self, edge):
        return self.conn.execute("SELECT fingerprint FROM vega_desk_edges WHERE id=?", (edge,)).fetchone()[0]

    def test_isolation_independent_evidence_and_incremental_removal(self):
        a, ar = self.source("a.md", "First independent passage.")
        _, br = self.source("b.md", "Second independent passage.")
        self.pack["rules"] = [self.rule(ar, "First independent passage."), self.rule(br, "Second independent passage.")]
        self.sync()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vega_desk_edges WHERE status='accepted'").fetchone()[0], 2)
        self.assertFalse(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='edges'").fetchone())
        a.unlink()
        self.pack["rules"] = [self.pack["rules"][1]]
        self.sync([a])
        self.assertEqual(self.conn.execute("SELECT source_path FROM vega_desk_evidence").fetchall(), [(br,)])
        self.sync()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vega_desk_edges").fetchone()[0], 1)

    def test_review_and_source_change_revalidation(self):
        path, rel = self.source("review.md", "Passage. Passage.")
        self.pack["rules"] = [self.rule(rel, "Passage.")]
        self.sync()
        edge, status = self.conn.execute("SELECT id,status FROM vega_desk_edges").fetchone()
        self.assertEqual(status, "queued")
        v.resolve_review(self.conn, edge, "accepted", note="checked both occurrences", expected_fingerprint=self.fingerprint(edge), root=self.root, pack_path=self.pack_path)
        self.assertEqual(self.conn.execute("SELECT status FROM vega_desk_edges").fetchone()[0], "accepted")
        path.write_text("Passage. Passage. changed", encoding="utf-8")
        self.sync([path])
        self.assertEqual(self.conn.execute("SELECT status FROM vega_desk_edges").fetchone()[0], "queued")
        path.write_text("No evidence remains.", encoding="utf-8")
        with self.assertRaises(ValueError):
            v.resolve_review(self.conn, edge, "accepted", note="no", expected_fingerprint=self.fingerprint(edge), root=self.root, pack_path=self.pack_path)

    def test_declaration_new_unlisted_source_and_designated_root(self):
        content = "---\nvega_edges:\n  - from: signal\n    to: risk\n    predicate: vega_confirms\n    kind: doctrine\n    quote: Signal confirms Risk.\n---\nSignal confirms Risk.\n"
        path, rel = self.source("new.md", content)
        self.sync([path])
        self.assertTrue(all(r[0] == "accepted" for r in self.conn.execute("SELECT status FROM vega_desk_edges").fetchall()))
        elsewhere = self.root / "VEGA" / "unapproved.md"
        elsewhere.write_text(content, encoding="utf-8")
        self.conn.execute("INSERT INTO nodes(id,path) VALUES (?,?)", ("outside", "VEGA/unapproved.md"))
        self.sync([elsewhere])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vega_desk_edges").fetchone()[0], 1)
        # The quoted header never counts as evidence: remove only body.
        path.write_text(content.rsplit("Signal confirms Risk.", 1)[0] + "Absent.\n", encoding="utf-8")
        self.sync([path])
        self.assertEqual(self.conn.execute("SELECT status FROM vega_desk_edges").fetchone()[0], "queued")

    def test_multiple_targets_not_conflicts_and_context_required(self):
        _, rel = self.source("multiple.md", "A. B.")
        self.pack["rules"] = [self.rule(rel, "A."), self.rule(rel, "B.", to="risk2")]
        self.sync()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vega_desk_edges WHERE status='accepted'").fetchone()[0], 2)
        self.pack["rules"][0]["kind"] = "backtest"
        with self.assertRaises(v.PackError):
            self.sync()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vega_desk_edges").fetchone()[0], 2)

    def test_flag_marker_and_rollback(self):
        marker = self.root / "vega_desk.enabled"
        with patch.object(v, "ENABLE_MARKER", marker), patch.dict(os.environ, {}, clear=True):
            self.assertFalse(v.enabled())
            marker.touch()
            self.assertTrue(v.enabled())
            os.environ["ARCTURION_VEGA_DESK"] = "0"
            self.assertFalse(v.enabled())
            os.environ["ARCTURION_VEGA_DESK"] = "1"
            marker.unlink()
            self.assertTrue(v.enabled())
        self.conn.commit()
        self.sync()
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertIsNone(self.conn.execute("SELECT name FROM sqlite_master WHERE name='vega_desk_edges'").fetchone())

    def test_literal_statement_is_narrow_and_unlisted(self):
        path, _ = self.source("literal.md", "Signal confirms Risk.\nIf Signal confirms Risk, wait.\nSignal and Risk are related.\n")
        self.sync([path])
        rows = self.conn.execute("SELECT e.kind,e.status,v.quote FROM vega_desk_edges e JOIN vega_desk_evidence v ON v.id=e.evidence_id").fetchall()
        self.assertEqual(rows, [("hypothesis", "accepted", "Signal confirms Risk.")])

    def test_explicit_conflict_reopens_existing_edge(self):
        first, ar = self.source("one.md", "Signal confirms Risk.")
        second, br = self.source("two.md", "Other evidence.")
        self.pack["rules"] = [self.rule(ar, "Signal confirms Risk.")]
        self.sync()
        self.pack["rules"].append(self.rule(br, "Other evidence.", to="risk2", conflicting=True))
        self.sync([second])
        rows = self.conn.execute("SELECT status FROM vega_desk_edges").fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(row[0] == "queued" for row in rows))

    def test_reviews_restore_only_same_fingerprint(self):
        path, rel = self.source("receipt.md", "Evidence. Evidence.")
        self.pack["rules"] = [self.rule(rel, "Evidence.")]
        self.sync()
        edge = self.conn.execute("SELECT id FROM vega_desk_edges").fetchone()[0]
        v.resolve_review(self.conn, edge, "rejected", note="insufficient", expected_fingerprint=self.fingerprint(edge), root=self.root, pack_path=self.pack_path)
        receipt = v.export_reviews(self.conn)
        self.conn.execute("UPDATE vega_desk_review_queue SET resolution=NULL,reviewed_fingerprint=NULL,note=NULL")
        self.conn.execute("UPDATE vega_desk_edges SET status='queued'")
        self.assertEqual(v.import_reviews(self.conn, receipt), {"restored": 1, "skipped": 0})
        path.write_text("Evidence. Evidence. New details.", encoding="utf-8")
        self.sync([path])
        self.assertEqual(self.conn.execute("SELECT status FROM vega_desk_edges").fetchone()[0], "queued")
        self.assertEqual(v.import_reviews(self.conn, receipt), {"restored": 0, "skipped": 1})

    def test_symlinks_and_irrelevant_event_fail_closed(self):
        outside = self.root / "private.md"
        outside.write_text("Signal confirms Risk.", encoding="utf-8")
        link = self.shelf / "link.md"
        link.symlink_to(outside)
        self.sync()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vega_desk_edges").fetchone()[0], 0)
        self.assertEqual(self.sync([self.root / "unrelated.md"])["unchanged"], True)
        with self.assertRaises(v.PackError):
            v._absolute_source(self.root, link.relative_to(self.root).as_posix())

    def test_watcher_scope_and_removal_event(self):
        path, _ = self.source("watched.md", "Signal confirms Risk.")
        protected = self.shelf / "Founder" / "secret.md"
        protected.parent.mkdir()
        protected.write_text("private", encoding="utf-8")
        with patch.object(watch.C, "DESKTOP_ROOT", self.root), patch.object(v, "enabled", return_value=True):
            self.assertTrue(watch.watch_event_allowed(str(path), []))
            # VEGA protected events are inventory-only; flush must not read them.
            self.assertTrue(watch.watch_event_allowed(str(protected), []))
            self.assertFalse(watch.watch_event_allowed(str(self.root / "unrelated.md"), []))
            self.assertIn("--event=Removed", watch.watch_command([str(self.shelf)]))

    def test_negative_claim_quarantines_both_and_withdrawal(self):
        for first_negative in (False, True):
            with self.subTest(first_negative=first_negative):
                positive, _ = self.source("positive.md", "Signal confirms Risk.")
                negative, _ = self.source("negative.md", "Signal does not confirm Risk.")
                first, second = (negative, positive) if first_negative else (positive, negative)
                second.rename(second.with_suffix(".hidden"))
                self.sync([first])
                second.with_suffix(".hidden").rename(second)
                self.sync([second])
                rows = self.conn.execute("SELECT e.id,e.status,q.reason FROM vega_desk_edges e JOIN vega_desk_review_queue q ON e.id=q.edge_id").fetchall()
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(row[1] == "queued" for row in rows))
                neg_id = next(row[0] for row in rows if "negative_assertion" in row[2])
                with self.assertRaises(ValueError):
                    v.resolve_review(self.conn, neg_id, "accepted", note="bad", expected_fingerprint=self.fingerprint(neg_id), root=self.root, pack_path=self.pack_path)
                negative.unlink()
                self.sync([negative])
                remaining, status = self.conn.execute("SELECT id,status FROM vega_desk_edges").fetchone()
                self.assertEqual(status, "queued")
                v.resolve_review(self.conn, remaining, "accepted", note="rechecked after withdrawal", expected_fingerprint=self.fingerprint(remaining), root=self.root, pack_path=self.pack_path)
                positive.unlink()
                self.sync([positive])

    def test_scoped_ontology_and_strict_flags(self):
        self.assertIn("vega_confirms", ontology.desk_vocabulary("VEGA", self.pack)["predicates"])
        self.assertNotIn("vega_confirms", validator.PREDICATE_CANON)
        self.assertTrue(any(x.code == "UNKNOWN_PREDICATE" for x in validator.validate_triple({"from": "x", "to": "y", "predicate": "vega_confirms"})))
        with self.assertRaises(ValueError):
            ontology.desk_vocabulary("OTHER", self.pack)
        _, rel = self.source("flag.md", "Evidence.")
        for flag in ("inferred", "ambiguous", "conflicting"):
            with self.subTest(flag=flag):
                self.pack["rules"] = [self.rule(rel, "Evidence.", **{flag: "true"})]
                with self.assertRaises(v.PackError):
                    validator.validate_desk_pack("VEGA", self.pack)

    def test_ungrounded_declaration_and_stale_review_are_blocked(self):
        content = "---\nvega_edges:\n  - from: signal\n    to: risk\n    predicate: vega_confirms\n    kind: doctrine\n    quote: There is no evidence of confirmation in this example.\n---\nThere is no evidence of confirmation in this example.\n"
        path, _ = self.source("misleading.md", content)
        self.sync([path])
        listing = v.list_reviews(self.conn)
        row = next(r for r in listing if r["source_path"] and r["source_path"].endswith("misleading.md"))
        self.assertIn("semantic_review_required", row["reason"])
        path.write_text(content + "New context.\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed since review"):
            v.resolve_review(self.conn, row["edge_id"], "accepted", note="reviewed", expected_fingerprint=row["fingerprint"], root=self.root, pack_path=self.pack_path)

    def test_direct_quote_inside_a_qualified_sentence_is_not_grounding(self):
        content = "---\nvega_edges:\n  - from: signal\n    to: risk\n    predicate: vega_confirms\n    kind: doctrine\n    quote: Signal confirms Risk.\n---\nThe assertion Signal confirms Risk. is not established.\n"
        path, _ = self.source("fragment.md", content)
        self.sync([path])
        row = self.conn.execute("SELECT status FROM vega_desk_edges").fetchone()
        self.assertEqual(row, ("queued",))
        self.assertIn("semantic_review_required", v.list_reviews(self.conn)[0]["reason"])

    def test_explicit_uncertainty_cannot_be_bypassed_by_literal(self):
        template = "---\nvega_edges:\n  - from: signal\n    to: risk\n    predicate: vega_confirms\n    kind: doctrine\n    quote: Signal confirms Risk.\n    inferred: %s\n---\nSignal confirms Risk.\n"
        path, _ = self.source("uncertain.md", template % "true")
        self.sync([path])
        rows = self.conn.execute("SELECT id,status FROM vega_desk_edges").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], "queued")
        edge = rows[0][0]
        v.resolve_review(self.conn, edge, "rejected", note="not established", expected_fingerprint=self.fingerprint(edge), root=self.root, pack_path=self.pack_path)
        self.sync([path])
        self.assertEqual(self.conn.execute("SELECT status FROM vega_desk_edges").fetchall(), [("rejected",)])
        path.write_text(template % '"true"', encoding="utf-8")
        self.sync([path])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vega_desk_edges").fetchone()[0], 0)
        self.assertTrue(any("invalid_declaration" in r["reason"] for r in v.list_reviews(self.conn)))
        path.write_text("Signal confirms Risk.\n", encoding="utf-8")
        self.sync([path])
        self.sync([path])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vega_desk_edges").fetchone()[0], 0)
        receipt = v.export_reviews(self.conn)
        fresh = sqlite3.connect(":memory:")
        self.addCleanup(fresh.close)
        fresh.execute("CREATE TABLE nodes(id TEXT PRIMARY KEY,path TEXT,content_hash TEXT)")
        rel = path.relative_to(self.root).as_posix()
        fresh.execute("INSERT INTO nodes(id,path) VALUES (?,?)", (rel, rel))
        v.sync(fresh, root=self.root, pack_path=self.pack_path)
        self.assertEqual(fresh.execute("SELECT count(*) FROM vega_desk_edges").fetchone()[0], 1)
        v.import_reviews(fresh, receipt)
        self.assertEqual(fresh.execute("SELECT count(*) FROM vega_desk_edges").fetchone()[0], 0)
        v.sync(fresh, root=self.root, pack_path=self.pack_path)
        self.assertEqual(fresh.execute("SELECT count(*) FROM vega_desk_edges").fetchone()[0], 0)

    def test_registered_multiword_negations_and_negative_label(self):
        pack = json.loads(v.PACK_PATH.read_text(encoding="utf-8"))
        lines = [
            "Retired 30-minute leveraged ETF ORB does not require the specified confirmation Opening range.",
            "Retired 30-minute leveraged ETF ORB never requires the specified confirmation Opening range.",
            "Retired 30-minute leveraged ETF ORB is not invalidated by the specified condition Opening range.",
            "Retired 30-minute leveraged ETF ORB is never invalidated by the specified condition Opening range.",
        ]
        for line in lines:
            with self.subTest(line=line):
                rows, _ = v._literal_assertions(line, "VEGA/example.md", pack)
                self.assertTrue(any(r.get("_negative") and r.get("conflicting") for r in rows))
        rows, _ = v._literal_assertions("Relative strength does not determine the timing of the target Opening range.", "VEGA/example.md", pack)
        self.assertTrue(any(r["predicate"] == "vega_does_not_time" and not r.get("_negative") for r in rows))

    def test_declarations_and_opposite_evidence_in_macro_and_crypto(self):
        macro = self.root / "VEGA/04 🌐 Markets & Macro"
        crypto = self.root / "VEGA/05 ₿ Crypto & DeFi"
        macro.mkdir(parents=True)
        crypto.mkdir(parents=True)
        positive, negative = macro / "macro.md", crypto / "crypto.md"
        positive.write_text("---\nvega_edges:\n  - from: signal\n    to: risk\n    predicate: vega_confirms\n    kind: doctrine\n    quote: Signal confirms Risk.\n---\nSignal confirms Risk.\n", encoding="utf-8")
        for path in (positive, negative):
            rel = path.relative_to(self.root).as_posix()
            self.conn.execute("INSERT INTO nodes(id,path) VALUES (?,?)", (rel, rel))
        self.sync([positive])
        self.assertEqual(self.conn.execute("SELECT status FROM vega_desk_edges").fetchall(), [("accepted",)])
        negative.write_text("Signal does not confirm Risk.\n", encoding="utf-8")
        self.sync([negative])
        self.assertEqual(self.conn.execute("SELECT status FROM vega_desk_edges ORDER BY status").fetchall(), [("queued",), ("queued",)])
        negative.unlink()
        self.sync([negative])
        self.assertEqual(self.conn.execute("SELECT status FROM vega_desk_edges").fetchall(), [("queued",)])


if __name__ == "__main__":
    unittest.main()
