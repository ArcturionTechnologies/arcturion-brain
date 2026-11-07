"""Temporary-index HTTP integration tests. No production index/service is opened."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.parse import urlencode

from engine import config as C, db as DB, capacity_admission as A
from engine.http_server import Handler


class BrainChangeHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / "temp.sqlite"
        self.registry = self.root / "business.json"
        self.personal = self.root / "personal.json"
        self.registry.write_text(json.dumps([
            {"id": "business-one", "capacity": "business", "hq_admitted": True,
             "read_roots": [str(self.root / "business")]},
            {"id": "not-admitted", "capacity": "business", "hq_admitted": False,
             "read_roots": [str(self.root / "unknown")]}]))
        self.personal.write_text(json.dumps([
            {"id": "personal-one", "capacity": "personal", "assignment_admitted": True,
             "read_roots": [str(self.root / "personal")]}]))
        self.patches = [
            patch.object(C, "BRAIN_DB", self.path),
            patch.object(C, "CHANGE_FEED_ENABLED", False),
            patch.object(A, "REGISTRY_PATH", self.registry),
            patch.object(A, "PERSONAL_ASSIGNMENTS_PATH", self.personal),
            patch.object(A, "SHARED_SOURCES", ()),
            patch.object(A, "BUSINESS_GOVERNING_SOURCES", ()),
            patch("engine.memory_policy.load_policy",
                  return_value=SimpleNamespace(data={}, allows=lambda node, resolve: True))]
        for p in self.patches:
            p.start()
        self.conn = DB.connect(self.path)
        DB.init_db(self.conn)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.conn.close()
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def request(self, query="capacity=business", headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            client.request("GET", "/changes?" + query, headers=headers or {})
            response = client.getresponse()
            return response.status, dict(response.getheaders()), json.loads(response.read())
        finally:
            client.close()

    def node(self, ref, capacity="business", **extra):
        row = dict(id=ref, type="Document", family="document", title=ref,
                   path=str(self.root / capacity / (ref + ".md")), owner="ALTAIR",
                   status="active", content_hash=ref, last_modified="2026-11-05T19:00:00Z",
                   body_excerpt="BODY-SENTINEL", summary="SUMMARY-SENTINEL",
                   metadata={"capacity": capacity})
        row.update(extra)
        DB.upsert_node(self.conn, row)
        self.conn.commit()

    def enable_temporary_index(self):
        C.CHANGE_FEED_ENABLED = True
        DB.init_db(self.conn)

    def test_disabled_init_and_route_do_not_install_or_open_read_connection(self):
        self.assertIsNone(self.conn.execute(
            "SELECT name FROM sqlite_master WHERE name='node_change_journal'").fetchone())
        with patch.object(A, "scoped_connection", side_effect=AssertionError("must not connect")):
            status, _, body = self.request()
        self.assertEqual(status, 503)
        self.assertEqual(body["error"], "change_feed_disabled")
        C.CHANGE_FEED_ENABLED = True
        original = self.server.server_address
        self.server.server_address = ("0.0.0.0", original[1])
        try:
            with patch.object(A, "scoped_connection", side_effect=AssertionError("must not connect")):
                status, _, body = self.request()
        finally:
            self.server.server_address = original
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "loopback_required")

    def test_cross_origin_and_public_host_rebinding_rejected_without_wildcard_cors(self):
        self.enable_temporary_index()
        self.node("allowed")
        for headers in ({"Origin": "https://untrusted.example"}, {"Origin": "null"}, {"Origin": ""},
                        {"Host": "untrusted.example:" + str(self.server.server_port)},
                        {"Host": "127.0.0.1:1"}, {"Host": "localhost.evil:" + str(self.server.server_port)}):
            with self.subTest(headers=headers):
                status, response_headers, _ = self.request(headers=headers)
                self.assertEqual(status, 403)
                self.assertNotIn("Access-Control-Allow-Origin", response_headers)
        for host in ("127.0.0.1", "localhost"):
            status, headers, body = self.request(headers={"Host": host + ":" + str(self.server.server_port)})
            self.assertEqual(status, 200)
            self.assertEqual(body["records"][0]["id"], "allowed")
            self.assertNotIn("Access-Control-Allow-Origin", headers)
            self.assertEqual(headers["Cache-Control"], "no-store")

        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            client.putrequest("GET", "/changes?capacity=business", skip_host=True)
            client.putheader("Host", "127.0.0.1:" + str(self.server.server_port))
            client.putheader("Host", "untrusted.example:" + str(self.server.server_port))
            client.endheaders()
            response = client.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        finally:
            client.close()
        # Native HTTP clients send no Origin and generate the exact loopback Host.
        from urllib.request import build_opener, ProxyHandler, Request
        opener = build_opener(ProxyHandler({}))
        with opener.open(Request("http://127.0.0.1:" + str(self.server.server_port)
                                 + "/changes?capacity=business&limit=1",
                                 headers={"Accept": "application/json"}), timeout=5) as response:
            self.assertEqual(json.loads(response.read())["records"][0]["id"], "allowed")

    def test_explicit_init_http_projection_current_admission_and_privacy(self):
        self.node("historical")
        self.enable_temporary_index()
        epoch = self.conn.execute("SELECT epoch FROM change_feed_config").fetchone()[0]
        DB.init_db(self.conn)
        self.assertEqual(epoch, self.conn.execute("SELECT epoch FROM change_feed_config").fetchone()[0])
        self.node("personal", "personal")
        self.node("unclassified", "unknown", metadata={})
        self.node("contrary", metadata={"capacity": "personal"})
        self.node("blocked")
        self.conn.execute(
            "INSERT INTO node_retrieval_quarantine(node_id,content_hash,reason,scope) VALUES(?,?,?,?)",
            ("blocked", "blocked", "test", "all"))
        self.conn.commit()
        status, headers, result = self.request()
        self.assertEqual(status, 200)
        self.assertIn("partial", headers["X-Brain-Coverage"])
        self.assertEqual([r["id"] for r in result["records"]], ["historical"])
        self.assertTrue(result["records"][0]["historical"])
        self.assertNotIn("SENTINEL", json.dumps(result))
        # Re-read the actual current owner manifest; a revoked scope cannot use its old cursor.
        self.registry.write_text(json.dumps([]))
        status, _, result = self.request(urlencode({
            "capacity": "business", "cursor": result["next_cursor"]}))
        self.assertEqual(status, 400)
        self.assertIn("admitted", result["error"].lower())

    def test_verification_cursor_rechecks_exact_current_metadata_and_privacy_policy(self):
        self.enable_temporary_index()
        self.node("allowed")
        first = self.request()[2]["records"][0]
        query = urlencode({"capacity": "business", "cursor": first["verification_cursor"], "limit": 1})
        self.assertEqual(self.request(query)[2]["records"][0]["sequence"], first["sequence"])
        # A current privacy ban suppresses the existing node before any response.
        with patch("engine.memory_policy.load_policy", return_value=SimpleNamespace(
                data={}, allows=lambda node, resolve: False)):
            self.assertEqual(self.request(query)[2]["records"], [])
        self.node("allowed", content_hash="changed")
        edited = self.request(query)[2]["records"][0]
        self.assertNotEqual(edited["sequence"], first["sequence"])
        self.node("allowed", metadata={"capacity": "personal"})
        self.node("later")
        self.assertEqual([row["id"] for row in self.request(query)[2]["records"]], ["later"])
        self.conn.execute("DELETE FROM nodes WHERE id=\'later\'")
        self.conn.commit()
        self.assertEqual(self.request(query)[2]["records"], [])

    def test_strict_route_selectors_and_scope_bound_cursor(self):
        self.enable_temporary_index()
        for query in ("", "capacity=", "capacity=other", "capacity=business&capacity=personal",
                      "capacity=business&project_id=", "capacity=business&root=/tmp",
                      "capacity=business&project_id=not-admitted", "capacity=business&limit=201",
                      "capacity=business&limit=0", "capacity=business&cursor=",
                      "capacity=business&dangling"):
            with self.subTest(query=query):
                self.assertEqual(self.request(query)[0], 400)
        self.node("business")
        status, _, first = self.request("capacity=business&project_id=business-one&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(first["project_id"], "business-one")
        status, _, result = self.request(urlencode({
            "capacity": "personal", "cursor": first["next_cursor"]}))
        self.assertEqual(status, 400)
        self.assertIn("scope", result["error"].lower())

    def test_excluded_frontier_and_restart_preserve_cursor_then_backdated_change(self):
        self.enable_temporary_index()
        for i in range(205):
            self.node("private-" + str(i), "personal")
        self.node("allowed")
        status, _, first = self.request("capacity=business&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(first["records"], [])
        self.assertTrue(first["has_more"])
        # New connections and server replacement read the persistent cursor generation.
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        status, _, second = self.request(urlencode({
            "capacity": "business", "cursor": first["next_cursor"], "limit": 1}))
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in second["records"]], ["allowed"])
        self.node("allowed", content_hash="edited", last_modified="2020-01-01T00:00:00Z")
        status, _, third = self.request(urlencode({
            "capacity": "business", "cursor": second["next_cursor"]}))
        self.assertEqual(status, 200)
        self.assertEqual(third["records"][0]["content_hash"], "edited")
        self.assertFalse(third["records"][0]["historical"])
        self.assertEqual(self.request(urlencode({
            "capacity": "business", "cursor": third["next_cursor"]}))[2]["records"], [])


if __name__ == "__main__":
    unittest.main()

