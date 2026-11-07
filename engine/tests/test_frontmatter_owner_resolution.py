"""Focused ownership resolution tests using canonical temporary Brain DB fixtures."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from .. import db as DB
from .. import graph_builder as GB
from ..maintenance import edge_endpoint_resolve as ER


PRODUCER = "STATIC_FRONTMATTER"
SPAN = "frontmatter:owner"


class FrontmatterOwnerResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="frontmatter_owner_")
        self.db_path = Path(self.temp.name) / "fixture.sqlite"
        self.conn = DB.connect(self.db_path)
        DB.init_db(self.conn)
        self._node("doc_one", "Document", "document", "One")
        self._node("doc_two", "Document", "document", "Two")
        self._node("doc_three", "Document", "document", "Three")
        self._node("doc_four", "Document", "document", "Four")
        self._node("owner_a", "Person", "entity", "Canonical Owner A")
        self._node("owner_b", "Person", "entity", "Canonical Owner B")
        self._node("service_name", "Service", "automation", "Service Owner",
                   "/repo/owners/Service Owner.md")

    def tearDown(self) -> None:
        self.conn.close()
        self.temp.cleanup()

    def _node(self, node_id: str, node_type: str, family: str, title: str,
              path: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO nodes (id,type,family,title,path) VALUES (?,?,?,?,?)",
            (node_id, node_type, family, title, path),
        )

    def _alias(self, alias: str, canonical_id: str) -> None:
        self.conn.execute(
            "INSERT INTO aliases (alias,canonical_id) VALUES (?,?)",
            (alias, canonical_id),
        )
        self.conn.commit()

    def _edge(self, source: str, target: str, *, predicate: str = "owned_by",
              asserted_by: str = PRODUCER, evidence_span: str = SPAN) -> int:
        cur = self.conn.execute(
            "INSERT INTO edges (from_id,to_id,predicate,family,asserted_by,"
            "asserted_at,evidence_span,metadata) VALUES (?,?,?,?,?,?,?,?)",
            (source, target, predicate, "structure", asserted_by,
             "2026-01-01T00:00:00Z", evidence_span, "{}"),
        )
        edge_id = int(cur.lastrowid)
        self.conn.commit()
        return edge_id

    def _rows(self) -> list[tuple]:
        return [tuple(row) for row in self.conn.execute(
            "SELECT id,from_id,to_id,predicate,family,weight,confidence,asserted_by,"
            "asserted_at,evidence_span,metadata FROM edges ORDER BY id"
        )]

    def _resolve(self, *, dry_run: bool = False, commit: bool = True) -> dict:
        return ER.run(
            self.conn,
            ["owned_by"],
            dry_run=dry_run,
            asserted_by=[PRODUCER],
            evidence_spans=[SPAN],
            mechanisms=["alias_table"],
            commit=commit,
        )

    def test_exact_filters_exclude_other_producer_evidence_and_predicate(self) -> None:
        self._alias("A. Owner", "owner_a")
        self._edge("doc_one", "A. Owner")
        self._edge("doc_two", "A. Owner", asserted_by="OTHER_PRODUCER")
        self._edge("doc_three", "A. Owner", evidence_span="frontmatter:author")
        self._edge("doc_four", "A. Owner", predicate="authored_by")
        before = self._rows()

        result = self._resolve()

        self.assertEqual(len(result["inserted_edge_ids"]), 1)
        self.assertEqual(self._rows()[:len(before)], before)
        twins = self.conn.execute(
            "SELECT from_id,to_id,metadata FROM edges WHERE id=?",
            (result["inserted_edge_ids"][0],),
        ).fetchone()
        self.assertEqual((twins["from_id"], twins["to_id"]), ("doc_one", "owner_a"))
        self.assertEqual(json.loads(twins["metadata"])["resolved_from"]["mechanism"],
                         "alias_table")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM edges WHERE to_id='owner_a'").fetchone()[0],
            1,
        )

    def test_alias_only_scope_refuses_name_and_path_matches(self) -> None:
        self._edge("doc_one", "Service Owner")
        self._edge("doc_two", "owners/Service Owner.md")

        result = self._resolve()

        self.assertEqual(result["inserted_edge_ids"], [])
        self.assertEqual(len(self._rows()), 2)

    def test_casefolded_conflicting_aliases_refuse_even_with_name_match(self) -> None:
        self._node("service_alias", "Service", "automation", "Conflict Name")
        self._node("service_other", "Service", "automation", "Other Target")
        self._alias("Conflict Name", "service_alias")
        self._alias("conflict name", "service_other")
        self._edge("doc_one", "Conflict Name")

        result = ER.run(
            self.conn, ["owned_by"], asserted_by=[PRODUCER],
            evidence_spans=[SPAN],
            mechanisms=["alias_table", "name_identity"], commit=False,
        )

        self.assertEqual(result["inserted_edge_ids"], [])
        self.assertEqual(len(self._rows()), 1)
        self.assertIn("Conflict Name", result["refused_dangling_ids"]["owned_by"])

    def test_case_variants_to_same_canonical_resolve(self) -> None:
        self._alias("Shared Alias", "owner_a")
        self._alias("shared alias", "owner_a")
        self._edge("doc_one", "SHARED ALIAS")

        result = self._resolve()

        self.assertEqual(len(result["inserted_edge_ids"]), 1)
        twin = self.conn.execute(
            "SELECT to_id,metadata FROM edges WHERE id=?",
            (result["inserted_edge_ids"][0],),
        ).fetchone()
        self.assertEqual(twin["to_id"], "owner_a")
        self.assertEqual(json.loads(twin["metadata"])["resolved_from"]["mechanism"],
                         "alias_table")

    def test_unmaterialized_alias_canonical_refuses_name_fallback(self) -> None:
        self._alias("Service Owner", "missing_owner_node")
        self._edge("doc_one", "Service Owner")

        result = ER.run(
            self.conn, ["owned_by"], asserted_by=[PRODUCER],
            evidence_spans=[SPAN],
            mechanisms=["alias_table", "name_identity"], commit=False,
        )

        self.assertEqual(result["inserted_edge_ids"], [])
        self.assertIn("Service Owner", result["refused_dangling_ids"]["owned_by"])

    def test_dry_run_reports_candidates_without_database_changes(self) -> None:
        self._alias("Dry Run Owner", "owner_a")
        self._edge("doc_one", "Dry Run Owner")
        before = self._rows()

        result = self._resolve(dry_run=True)

        self.assertEqual(result["inserted_edge_ids"], [])
        self.assertGreaterEqual(result["resolved_edges"], 1)
        self.assertEqual(self._rows(), before)

    def test_many_to_one_resolution_returns_only_inserted_twin_ids(self) -> None:
        self._alias("Owner Alias One", "owner_a")
        self._alias("Owner Alias Two", "owner_a")
        first_id = self._edge("doc_one", "Owner Alias One")
        second_id = self._edge("doc_one", "Owner Alias Two")
        before = self._rows()

        result = self._resolve()

        self.assertEqual(result["resolved_edges"], 2)
        self.assertEqual(result["inserted_edges"], 1)
        self.assertEqual(len(result["inserted_edge_ids"]), 1)
        self.assertEqual(self._rows()[:len(before)], before)
        twin = self.conn.execute(
            "SELECT id,from_id,to_id,metadata FROM edges WHERE id=?",
            (result["inserted_edge_ids"][0],),
        ).fetchone()
        self.assertEqual((twin["from_id"], twin["to_id"]), ("doc_one", "owner_a"))
        self.assertEqual(
            json.loads(twin["metadata"])["resolved_from"]["source_edge_id"],
            first_id,
        )
        self.assertNotEqual(first_id, second_id)

    def test_repeat_run_inserts_no_duplicate_twins(self) -> None:
        self._alias("Repeat Owner", "owner_a")
        self._edge("doc_one", "Repeat Owner")
        first = self._resolve()
        second = self._resolve()

        self.assertEqual(len(first["inserted_edge_ids"]), 1)
        self.assertEqual(second["inserted_edge_ids"], [])
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM edges WHERE from_id='doc_one' AND to_id='owner_a'"
            ).fetchone()[0],
            1,
        )

    def test_caller_rollback_removes_uncommitted_twins_and_preserves_sources(self) -> None:
        self._alias("Rollback Owner", "owner_a")
        self._edge("doc_one", "Rollback Owner")
        before = self._rows()

        result = self._resolve(commit=False)
        self.assertEqual(len(result["inserted_edge_ids"]), 1)
        self.conn.rollback()

        self.assertEqual(self._rows(), before)

    def test_simulated_insert_failure_leaves_rollback_to_caller(self) -> None:
        self._alias("Failure Owner", "owner_a")
        self._edge("doc_one", "Failure Owner")
        self._edge("doc_two", "Failure Owner")
        before = self._rows()
        self.conn.execute(
            "CREATE TRIGGER fail_second_twin BEFORE INSERT ON edges "
            "WHEN NEW.from_id='doc_two' AND NEW.to_id='owner_a' "
            "BEGIN SELECT RAISE(ABORT,'simulated insert failure'); END"
        )

        try:
            with self.assertRaises(sqlite3.IntegrityError):
                self._resolve(commit=False)
        finally:
            self.conn.rollback()

        self.assertEqual(self._rows(), before)

    def test_production_helper_uses_only_scoped_frontmatter_aliases(self) -> None:
        self._alias("Helper Owner", "owner_a")
        eligible = self._edge("doc_one", "Helper Owner")
        self._edge("doc_two", "Helper Owner", asserted_by="OTHER_PRODUCER")
        self._edge("doc_three", "Helper Owner", evidence_span="frontmatter:author")
        self._edge("doc_four", "Service Owner")
        before = self._rows()

        result = GB._resolve_frontmatter_owners(self.conn)

        self.assertEqual(result["scope"]["asserted_by"], (PRODUCER,))
        self.assertEqual(result["scope"]["evidence_spans"], (SPAN,))
        self.assertEqual(result["scope"]["mechanisms"], ("alias_table",))
        self.assertEqual(len(result["inserted_edge_ids"]), 1)
        twin = self.conn.execute(
            "SELECT metadata FROM edges WHERE id=?",
            (result["inserted_edge_ids"][0],),
        ).fetchone()
        self.assertEqual(
            json.loads(twin["metadata"])["resolved_from"]["source_edge_id"], eligible
        )
        self.assertEqual(self._rows()[:len(before)], before)
        self.conn.rollback()
        self.assertEqual(self._rows(), before)


if __name__ == "__main__":
    unittest.main()
