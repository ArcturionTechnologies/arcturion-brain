"""Hermetic capacity-bound retrieval tests: no live DB, source bodies or model calls."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
from .. import config as C, db as DB, brain as B
from .. import capacity_admission as A
from .. import vector_seed as VS, rank_signals as RS, mcp_server as MCP
from .. import http_server as HTTP

class CapacityIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.business = self.root / "business"
        self.personal = self.root / "personal"
        self.other = self.root / "other"
        self.registry = self.root / "registry.json"
        self.assignments = self.root / "personal.json"
        self.registry.write_text(json.dumps([
            {"id":"biz", "capacity":"business", "hq_admitted":True,
             "read_roots":[str(self.business)]},
            {"id":"unknown", "read_roots":[str(self.other)]},
            {"id":"private", "capacity":"personal", "hq_admitted":False,
             "read_roots":[str(self.personal)]}]))
        self.assignments.write_text(json.dumps([
            {"id":"person", "capacity":"personal", "assignment_admitted":True,
             "read_roots":[str(self.personal)]}]))
        self.patches = [
            patch.object(C, "BRAIN_DB", self.root / "brain.sqlite"),
            patch.object(C, "RETRIEVAL_V2", False),
            patch.object(A, "REGISTRY_PATH", self.registry),
            patch.object(A, "PERSONAL_ASSIGNMENTS_PATH", self.assignments),
            patch.object(A, "SHARED_SOURCES", (str(self.root / "shared.md"),)),
            # These tests pin the strict registry-only union; the Cmd 044 domain-neutral
            # retrieval default is covered by the test_domain_neutral_* methods below.
            patch.object(A, "STRICT_BUSINESS_UNION", True),
        ]
        for p in self.patches: p.start()
        RS.reset_cache(); VS.reset_cache()
        conn = DB.connect(); DB.init_db(conn)
        for nid, path, md in [
            ("biz", self.business/"record.md", {}),
            ("contrary", self.business/"private.md", {"capacity":"personal"}),
            ("person", self.personal/"record.md", {"capacity":"personal"}),
            ("unknown", self.other/"record.md", {}),
            ("shared", self.root/"shared.md", {"capacity":"shared"})]:
            DB.upsert_node(conn, {"id":nid, "type":"Source", "family":"document",
                "title":"Capacityfixture " + nid, "path":str(path), "owner":"NEMESIS",
                "status":"active", "governance_tier":0, "content_hash":None,
                "summary":"Capacityfixture synthetic " + nid,
                "body_excerpt":"SYNTHETIC_PAYLOAD_" + nid,
                "centrality":0.01, "created":None,
                "last_modified":"2026-10-20T12:00:00Z", "last_reviewed":None,
                "metadata":md})
        for dest in ("person","unknown","contrary","shared"):
            DB.insert_edge(conn, {"from":"biz","to":dest,"predicate":"depends_on",
                                "family":"structure","asserted_by":"TEST"})
        conn.commit(); conn.close()

    def tearDown(self):
        RS.reset_cache(); VS.reset_cache()
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    def test_query_and_neighbor_scope(self):
        pack = B.query("capacityfixture", radius=3)
        self.assertEqual({n["id"] for n in pack.nodes}, {"biz","shared"})
        self.assertTrue(all(e["to_id"] == "shared" for e in pack.edges))
        self.assertIn("capacity_scope:business", " ".join(pack.warnings))
        for nid in ("person","contrary","unknown"):
            self.assertNotIn("SYNTHETIC_PAYLOAD_"+nid, pack.text_bundle)
            self.assertFalse(B.query(nid, owner="NEMESIS", owner_strict=True).nodes)

    def test_payload_never_crosses_cursor_boundary(self):
        original_connect=DB.connect
        seen=[]
        def audited_connect(*args,**kwargs):
            conn=original_connect(*args,**kwargs)
            factory=conn.row_factory
            def audited_row(cursor,row):
                result=factory(cursor,row)
                if "body_excerpt" in result.keys():
                    seen.append(result["body_excerpt"])
                return result
            conn.row_factory=audited_row
            return conn
        with patch.object(DB,"connect",side_effect=audited_connect):
            B.query("capacityfixture",radius=3)
            B.capture_receipt(str(self.personal/"record.md"))
        self.assertIn("SYNTHETIC_PAYLOAD_biz",seen)
        for nid in ("person","unknown","contrary"):
            self.assertNotIn("SYNTHETIC_PAYLOAD_"+nid,seen)

    def test_admission_classifies_once_and_pins_metadata_snapshot(self):
        calls=[]
        original=A.AdmissionPolicy.allows
        def counted(policy,path,metadata):
            calls.append(path)
            return original(policy,path,metadata)
        with patch.object(A.AdmissionPolicy,"allows",counted):
            conn=B.scoped_connection()
            try:
                initial_calls=len(calls)
                self.assertGreater(initial_calls,0)
                for _ in range(3):
                    conn.execute("SELECT body_excerpt FROM nodes").fetchall()
                    conn.execute("SELECT * FROM edges").fetchall()
                self.assertEqual(len(calls),initial_calls)
                writer=DB.connect()
                writer.execute("UPDATE nodes SET metadata=?,body_excerpt=? WHERE id='biz'",
                               ('{"capacity":"personal"}',"SYNTHETIC_NEW_PRIVATE_PAYLOAD"))
                writer.commit(); writer.close()
                row=conn.execute("SELECT body_excerpt FROM nodes WHERE id='biz'").fetchone()
                self.assertEqual(row[0],"SYNTHETIC_PAYLOAD_biz")
            finally: conn.close()
            next_conn=B.scoped_connection()
            try: self.assertIsNone(next_conn.execute("SELECT body_excerpt FROM nodes WHERE id='biz'").fetchone())
            finally: next_conn.close()

    def test_candidate_prefilter_matches_full_policy_for_storage_variants(self):
        root=C.UNIVERSE_ROOT/"__capacity_fixture__/project"
        relative=root.relative_to(C.UNIVERSE_ROOT).as_posix()
        desktop_relative=root.relative_to(C.DESKTOP_ROOT).as_posix()
        records=[
            ("absolute",str(root/"a.md"),{}),
            ("relative",relative+"/b.md",{}),
            ("desktop",desktop_relative+"/c.md",{}),
            ("dot","./"+relative+"/d.md",{}),
            ("duplicate",str(root)+"//e.md",{}),
            ("desktop_dot","./"+desktop_relative+"/f.md",{}),
            ("sibling",str(root)+"-other/private.md",{}),
            ("traversal",str(root)+"/../private/g.md",{}),
            ("contrary",str(root/"p.md"),{"capacity":"personal"}),
        ]
        conn=DB.connect()
        for nid,path,metadata in records:
            DB.upsert_node(conn,{"id":"variant_"+nid,"type":"Source","family":"document",
                "path":path,"title":"Synthetic","summary":"Synthetic",
                "body_excerpt":"SYNTHETIC_VARIANT_"+nid,"metadata":metadata})
        conn.commit();conn.close()
        policy=A.AdmissionPolicy("business",(str(root),))
        results=[]
        for optimized in (False,True):
            conn=DB.connect()
            try:
                A.install_admission(conn,policy,A.stored_source_path,indexed_candidates=optimized)
                results.append({r[0] for r in conn.execute("SELECT id FROM nodes")})
            finally: conn.close()
        self.assertEqual(results[0],results[1])
        self.assertEqual(results[1],{"variant_"+nid for nid in
                         ("absolute","relative","desktop","dot","duplicate","desktop_dot")})

    def test_cli_capacity_contract(self):
        from io import StringIO
        with patch("sys.argv",["brain","query","capacityfixture","--capacity","personal",
                               "--project-id","person","--json"]), \
             patch.object(B,"query",return_value=B.ContextPack("capacityfixture")) as call, \
             patch("sys.stdout",new_callable=StringIO):
            B._cli()
        self.assertEqual(call.call_args.kwargs["capacity"],"personal")
        self.assertEqual(call.call_args.kwargs["project_id"],"person")

    def test_personal_and_project_narrowing(self):
        pack = B.query("capacityfixture", capacity="personal", project_id="person")
        self.assertEqual({n["id"] for n in pack.nodes}, {"person","shared"})
        with self.assertRaisesRegex(ValueError, "capacity"):
            B.query("capacityfixture", capacity="all")
        with self.assertRaisesRegex(ValueError, "admitted"):
            B.query("capacityfixture", project_id="unknown")
        with patch.object(A,"PERSONAL_ASSIGNMENTS_PATH", self.root/"missing.json"):
            with self.assertRaisesRegex(ValueError, "unavailable"):
                B.query("capacityfixture", capacity="personal")
        with patch.object(A,"REGISTRY_PATH", self.root/"missing.json"):
            with self.assertRaisesRegex(ValueError, "unavailable"):
                B.query("capacityfixture")

    def test_receipt_and_recency_scope(self):
        self.assertEqual(B.capture_receipt(str(self.personal/"record.md"))["status"], "empty")
        self.assertEqual(B.capture_receipt(str(self.business/"record.md"))["status"], "hit")
        pack = B.query("", order_by="recency")
        self.assertEqual({n["id"] for n in pack.nodes}, {"biz","shared"})

    def test_mcp_query_search_receipt_capacity(self):
        for name,args in [
            ("brain_query", {"topic":"capacityfixture","format":"json"}),
            ("brain_search", {"text":"capacityfixture"}),
            ("brain_capture_receipt", {"path":str(self.personal/"record.md")})]:
            result = MCP.handle({"id":1,"method":"tools/call","params":{"name":name,"arguments":args}})
            self.assertNotIn("error",result)
            text = json.dumps(result)
            self.assertNotIn("SYNTHETIC_PAYLOAD_person", text)
            self.assertNotIn(str(self.personal), text)
            self.assertNotIn("SYNTHETIC_PAYLOAD_unknown", text)
        result = MCP.handle({"id":1,"method":"tools/call","params":{"name":"brain_query",
            "arguments":{"topic":"capacityfixture","capacity":"all"}}})
        self.assertIn("capacity", result["error"]["message"])
        result = MCP.handle({"id":1,"method":"tools/call","params":{"name":"brain_search",
            "arguments":{"text":"capacityfixture","capacity":"personal","project_id":"person"}}})
        text = json.dumps(result)
        self.assertIn(str(self.personal),text)
        self.assertNotIn(str(self.business),text)

    def test_http_transports(self):
        server = ThreadingHTTPServer(("127.0.0.1",0), HTTP.Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)
        try:
            for route in ("/query?topic=capacityfixture&format=json",
                          "/search?text=capacityfixture", "/graph_subset?search=capacityfixture"):
                with urlopen(base+route) as response:
                    text=response.read().decode()
                self.assertNotIn("SYNTHETIC_PAYLOAD_person",text)
                self.assertNotIn("Capacityfixture person",text)
                self.assertNotIn("Capacityfixture unknown",text)
                self.assertNotIn("Capacityfixture contrary",text)
            request = Request(base+"/query", data=json.dumps({"topic":"capacityfixture",
                "capacity":"personal","project_id":"person"}).encode(),
                headers={"Content-Type":"application/json"})
            with urlopen(request) as response: text=response.read().decode()
            self.assertIn("SYNTHETIC_PAYLOAD_person",text)
            self.assertNotIn("SYNTHETIC_PAYLOAD_biz",text)
            for route in ("/query?topic=x&capacity=invalid","/search?text=x&capacity=invalid",
                          "/graph_subset?capacity=invalid"):
                with self.assertRaises(HTTPError) as result: urlopen(base+route)
                self.assertEqual(result.exception.code,400)
        finally:
            server.shutdown(); server.server_close(); thread.join(2)

    def test_vector_cache_subset_before_similarity(self):
        import numpy as np
        ids=["person","unknown","biz","shared","contrary"]
        matrix=np.zeros((5,C.EMBED_DIM),dtype=np.float32); matrix[:,0]=[1,.99,.7,.6,.98]
        cached=(ids,matrix,{nid:i for i,nid in enumerate(ids)},time.time())
        VS._MATRIX=cached
        class Model:
            def encode(self,*a,**kw):
                q=np.zeros((1,C.EMBED_DIM),dtype=np.float32); q[0,0]=1; return q
        with patch.object(VS,"_get_model",return_value=Model()):
            for capacity,project_id,expected in [
                ("business",None,{"biz","shared"}),
                ("personal","person",{"person","shared"}),
                ("business","biz",{"biz","shared"})]:
                conn=B.scoped_connection(capacity,project_id)
                try:
                    sims=VS.similarity(conn,"capacityfixture")
                    self.assertEqual({nid for nid,_ in sims.top(20)},expected)
                finally: conn.close()
                with patch.object(C, "RETRIEVAL_V2", True):
                    pack=B.query("capacityfixture",capacity=capacity,project_id=project_id)
                self.assertEqual({n["id"] for n in pack.nodes}, expected)
                self.assertIs(VS._MATRIX,cached)
        # A cold scoped load cannot replace the global all-capacity cache.
        VS._MATRIX=None
        conn=B.scoped_connection()
        try: VS._get_matrix(conn)
        finally: conn.close()
        self.assertIsNone(VS._MATRIX)

    def test_idf_cache_and_manifest_change(self):
        conn=DB.connect()
        row=dict(conn.execute("SELECT * FROM nodes WHERE id='person'").fetchone())
        row["metadata"]=json.loads(row["metadata"])
        row.update(id="person_extra",path=str(self.personal/"extra.md"))
        DB.upsert_node(conn,row); conn.commit(); conn.close()
        conn=B.scoped_connection()
        try:
            self.assertEqual(RS.corpus_size(conn),2)
            self.assertEqual(RS.document_frequency(conn,"capacityfixture"),2)
        finally: conn.close()
        conn=B.scoped_connection("personal","person")
        try:
            self.assertEqual(RS.corpus_size(conn),3)
            self.assertEqual(RS.document_frequency(conn,"capacityfixture"),3)
        finally: conn.close()
        # Revocation on disk applies next request, without waiting for a TTL.
        self.registry.write_text("[]")
        with self.assertRaisesRegex(ValueError,"admitted"): B.query("capacityfixture")

if __name__ == "__main__":
    unittest.main()

class DomainNeutralDefault(CapacityIntegration):
    """Cmd 044: the default business retrieval scope is domain-neutral (2026-11-05 fix)."""
    def setUp(self):
        super().setUp()
        neutral = patch.object(A, "STRICT_BUSINESS_UNION", False)
        neutral.start(); self.patches.append(neutral)
        conn = DB.connect()
        DB.upsert_node(conn, {"id":"telemetry", "type":"Event", "family":"event",
            "title":"Capacityfixture telemetry", "path":str(self.other/"t.json"), "owner":"NEMESIS",
            "status":"active", "governance_tier":0, "content_hash":None,
            "summary":"Capacityfixture synthetic telemetry", "body_excerpt":"SYNTHETIC_PAYLOAD_telemetry",
            "centrality":0.01, "created":None, "last_modified":"2026-10-20T12:00:00Z",
            "last_reviewed":None, "metadata":{"record_class":"machine_telemetry"}})
        conn.commit(); conn.close()

    def _admitted(self, **kw):
        conn = A.retrieval_connection(**kw)
        try:
            return {r[0] for r in conn.execute("SELECT id FROM capacity_admitted_ids")}
        finally:
            conn.close()

    # The strict-union expectations inherited from CapacityIntegration do not apply here.
    for _name in [n for n in dir(CapacityIntegration) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def test_domain_neutral_admits_unclassified_but_never_personal_contrary_or_telemetry(self):
        pack = B.query("capacityfixture", radius=3)
        self.assertEqual({n["id"] for n in pack.nodes}, {"biz", "unknown", "shared"})
        for nid in ("person", "contrary", "telemetry"):
            self.assertNotIn("SYNTHETIC_PAYLOAD_"+nid, pack.text_bundle)
        self.assertIn("domain-neutral-default", " ".join(pack.warnings))
        self.assertEqual(self._admitted(), {"biz", "unknown", "shared"})
        self.assertEqual(self._admitted(include_telemetry=True), {"biz", "unknown", "shared", "telemetry"})

    def test_project_id_and_strict_entry_point_stay_strict(self):
        pack = B.query("capacityfixture", radius=3, project_id="biz")
        self.assertEqual({n["id"] for n in pack.nodes}, {"biz", "shared"})
        conn = A.scoped_connection("business")
        try:
            ids = {r[0] for r in conn.execute("SELECT id FROM capacity_admitted_ids")}
        finally:
            conn.close()
        self.assertEqual(ids, {"biz", "shared"})
        self.assertNotIn("domain-neutral", A.coverage_warning("business"))

    def test_env_flag_restores_strict_union(self):
        with patch.object(A, "STRICT_BUSINESS_UNION", True):
            self.assertEqual(self._admitted(), {"biz", "shared"})

    def test_declared_personal_scope_fails_closed_when_malformed(self):
        self.assignments.write_text("not json")
        with self.assertRaises(ValueError):
            self._admitted()
        self.assignments.unlink()  # absent personal manifest = nothing declared there
        self.assertIn("unknown", self._admitted())
        self.assertNotIn("person", self._admitted())  # still excluded by its personal label

