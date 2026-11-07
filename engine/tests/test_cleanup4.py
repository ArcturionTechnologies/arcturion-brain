"""Cleanup 04: hermetic semantic, classification and admission regressions."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from .. import code_references as R, telemetry as T, db as DB, capacity_admission as A
from ..extractors.code import extract_code
from ..extractors.config_file import extract_config

def node(path, text):
    return {"id":path,"path":path,"title":Path(path).name,"type":"Module","family":"code",
            "metadata":{"python_analysis":R.analyze(text,path)},"content_hash":"fixture"}

def edges(source, *others):
    catalog = {}
    for n in (source,*others):catalog.setdefault(n["path"],[]).append(n)
    return R.resolved_edges(source,catalog)

def sites(es, expression):
    return [s for e in es for s in e["metadata"]["callsites"] if s["expression"]==expression]

class CodeReferences(unittest.TestCase):
    def test_distinct_same_name_methods_preserve_all_sites(self):
        n=node("p/a.py","class A:\n def send(self): pass\nclass B:\n def send(self): pass\nA.send(None)\nB.send(None)\nA.send(None)\n")
        es=edges(n);self.assertEqual(len(es),1)
        ss=es[0]["metadata"]["callsites"]
        self.assertEqual(len(ss),3)
        self.assertEqual({s["callee_qualified"] for s in ss},{"p/a.py::A.send","p/a.py::B.send"})
        self.assertFalse(es[0]["metadata"]["truncated"])

    def test_unknown_receivers_never_merge(self):
        n=node("p/a.py","def f(a,b):\n a.send()\n b.send()\n")
        es=edges(n);self.assertEqual(len(es),2)
        self.assertTrue(all(e["to"].startswith("unresolved_code_") for e in es))

    def test_import_alias_and_same_basename_other_project(self):
        n=node("one/main.py","from helper import go as run\nrun()\n")
        a=node("one/helper.py","def go(): pass\n")
        b=node("two/helper.py","def go(): pass\n")
        ss=sites(edges(n,a,b),"run")
        self.assertEqual(ss[0]["callee_qualified"],"one/helper.py::go")
        self.assertEqual(ss[0]["resolution"],"resolved")

    def test_import_requires_verified_symbol(self):
        n=node("p/main.py","from helper import absent\nabsent()\n")
        a=node("p/helper.py","def go(): pass\n")
        self.assertEqual(sites(edges(n,a),"absent")[0]["resolution"],"unresolved")

    def test_relative_import(self):
        n=node("p/pkg/main.py","from .helper import go as run\nrun()\n")
        a=node("p/pkg/helper.py","def go(): pass\n")
        init=node("p/pkg/__init__.py","")
        self.assertEqual(sites(edges(n,a,init),"run")[0]["callee_qualified"],"p/pkg/helper.py::go")
        self.assertEqual(sites(edges(n,a),"run")[0]["resolution"],"unresolved")

    def test_shadowing_parameter_assignment_and_export(self):
        text="from helper import go as run\ndef f(run):\n run()\ndef g():\n run=lambda:0\n run()\n"
        n=node("p/main.py",text);a=node("p/helper.py","def go(): pass\n")
        self.assertTrue(all(s["resolution"]=="unresolved" for s in sites(edges(n,a),"run")))
        n=node("p/main.py","from helper import go\ngo()\n")
        a=node("p/helper.py","def go(): pass\ngo = object()\n")
        self.assertEqual(sites(edges(n,a),"go")[0]["resolution"],"unresolved")

    def test_nested_self_and_external_unresolved(self):
        n=node("p/a.py","import nonexistent\ndef f():\n def nested(): pass\n nested()\nclass A:\n def m(self):\n  self.m()\nnonexistent.go()\n")
        for expression in ("nested","self.m","nonexistent.go"):
            self.assertEqual(sites(edges(n),expression)[0]["resolution"],"unresolved")

    def test_decorated_export_unresolved(self):
        n=node("p/main.py","from helper import go\ngo()\n")
        a=node("p/helper.py","@decorator\ndef go(): pass\n")
        self.assertEqual(sites(edges(n,a),"go")[0]["resolution"],"unresolved")

    def test_ambiguous_module_file_and_package(self):
        n=node("p/main.py","import helper\nhelper.go()\n")
        a=node("p/helper.py","def go(): pass\n")
        b=node("p/helper/__init__.py","def go(): pass\n")
        self.assertEqual(sites(edges(n,a,b),"helper.go")[0]["resolution_reason"],"ambiguous_module")

    def test_refresh_retires_bare_and_inverse_but_preserves_other_writer(self):
        c=sqlite3.connect(":memory:");c.row_factory=sqlite3.Row;DB.init_db(c)
        n=node("p/a.py","def go(): pass\ngo()\n");DB.upsert_node(c,n)
        for src,dst,pred,writer in [("p/a.py","go","calls","STATIC_AST"),
                                   ("go","p/a.py","called_by","INVERSE_DERIVATION"),
                                   ("p/a.py","go","calls","INVERSE_DERIVATION"),
                                   ("p/a.py","manual","depends_on","MANUAL")]:
            DB.insert_edge(c,{"from":src,"to":dst,"predicate":pred,"asserted_by":writer})
        with patch.object(R,"allowed_source",return_value=True): result=R.refresh(c);self.assertEqual(result["self"],1);self.assertEqual(result["cross_file"],0)
        self.assertEqual(c.execute("select count(*) from edges where to_id='go' or from_id='go'").fetchone()[0],0)
        self.assertEqual(c.execute("select count(*) from edges where asserted_by='MANUAL'").fetchone()[0],1)
        with patch.object(R,"allowed_source",return_value=True): R.refresh(c);self.assertEqual(c.execute("select count(*) from edges").fetchone()[0],2)
        c.close()

class AdditionalBindingRegressions(unittest.TestCase):
    def test_class_deletion_conditional_binding_global_write(self):
        cases=[
            ("class A:\n def f(self): pass\n del f\nA.f(None)\n","A.f"),
            ("class A:\n def f(self): pass\n if condition:\n  f=other\nA.f(None)\n","A.f"),
            ("def helper(): pass\ndef mutate():\n global helper\n helper=other\nhelper()\n","helper"),
        ]
        for text,expr in cases:
            with self.subTest(text=text):
                self.assertEqual(sites(edges(node("p/a.py",text)),expr)[0]["resolution"],"unresolved")

    def test_relative_import_cannot_escape_package(self):
        n=node("root/pkg/main.py","from ..target import go\ngo()\n")
        init=node("root/pkg/__init__.py","")
        target=node("root/target.py","def go(): pass\n")
        self.assertEqual(sites(edges(n,init,target),"go")[0]["resolution"],"unresolved")
        root=node("root/__init__.py","")
        self.assertEqual(sites(edges(n,init,target,root),"go")[0]["resolution"],"resolved")


class Telemetry(unittest.TestCase):
    def test_bounded_path_and_schema(self):
        path=T.PREFIXES[0]+"raw/"+"a"*64+".json"
        self.assertEqual(T.classify(path)["record_class"],"machine_telemetry")
        self.assertIsNone(T.classify("07 ⚙️ OPS/config/"+("a"*64)+".json",{"observed_at":1,"payload":{}}))
        self.assertIsNone(T.classify(T.PREFIXES[0]+"strategy_config.json",{"risk":1}))
        self.assertIsNone(T.classify(T.PREFIXES[0]+"../private.json",{"observed_at":1,"payload":{}}))
        self.assertIsNone(T.classify("ATLAS/"+path,{"observed_at":1,"payload":{}}))
        self.assertIsNotNone(T.classify(T.PREFIXES[1]+"receipt.json",{"started_at":1,"completed_at":2}))

    def test_versioned_hash_skip(self):
        n={"metadata":{"record_class":"machine_telemetry","classification_version":1}}
        self.assertFalse(T.extraction_current(n,"{}"))
        self.assertTrue(T.extraction_current(n,json.dumps(n["metadata"])))
        self.assertFalse(T.extraction_current({"metadata":{}},n["metadata"]))

    def test_default_vs_evidence_admission_and_private_boundaries(self):
        def run(include):
            c=sqlite3.connect(":memory:");DB.init_db(c)
            for nid,path,md in [("durable","/biz/a",{}),("telemetry","/biz/t",{"record_class":"machine_telemetry"}),
                                ("private","/biz/p",{"capacity":"personal"}),("unknown","/other/u",{})]:
                DB.upsert_node(c,{"id":nid,"path":path,"type":"Source","family":"document","metadata":md})
            c.commit()
            A.install_admission(c,A.AdmissionPolicy("business",("/biz",)),include_telemetry=include)
            found={r[0] for r in c.execute("select id from nodes")};c.close();return found
        self.assertEqual(run(False),{"durable"})
        self.assertEqual(run(True),{"durable","telemetry"})

class Charter(unittest.TestCase):
    def test_business_only_exact_source_and_contrary_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/"registry.json"
            p.write_text(json.dumps([{"id":"biz","capacity":"business","hq_admitted":True,"read_roots":["/biz"]}]))
            q=Path(tmp)/"personal.json"
            q.write_text(json.dumps([{"id":"person","capacity":"personal","assignment_admitted":True,"read_roots":["/personal"]}]))
            with patch.object(A,"REGISTRY_PATH",p),patch.object(A,"PERSONAL_ASSIGNMENTS_PATH",q):
                biz=A.load_admission("business");personal=A.load_admission("personal")
                charter=A.BUSINESS_GOVERNING_SOURCES[0]
                self.assertTrue(biz.allows(charter,{}))
                self.assertFalse(personal.allows(charter,{}))
                self.assertFalse(biz.allows(charter,{"capacity":"personal"}))
                self.assertFalse(biz.allows(charter+"x",{}))
                self.assertFalse(biz.allows("/unclassified/source.md",{}))

if __name__=="__main__":unittest.main()

class ReviewRegressions(unittest.TestCase):
    def test_review_shadowing_cases(self):
        cases=[
            ("def f(): pass\nclass A:\n def f(): pass\n x=f()\n","f"),
            ("def helper(): pass\ntry: pass\nexcept Exception as helper:\n helper()\n","helper"),
            ("def helper(): pass\nmatch x:\n case helper:\n  helper()\n","helper"),
            ("class A:\n def f(self): pass\n @deco\n def f(self): pass\nA.f(None)\n","A.f"),
            ("def helper(): pass\nif condition:\n def helper(): pass\nhelper()\n","helper"),
            ("from helper import go\nfrom other import *\ngo()\n","go"),
        ]
        for text,expr in cases:
            with self.subTest(text=text):
                self.assertEqual(sites(edges(node("p/a.py",text)),expr)[0]["resolution"],"unresolved")

    def test_unrelated_inverse_producer_preserved(self):
        rows=[
            {"id":1,"from_id":"a","to_id":"x","predicate":"calls","asserted_by":"STATIC_AST"},
            {"id":2,"from_id":"x","to_id":"a","predicate":"called_by","asserted_by":"INVERSE_DERIVATION"},
            {"id":3,"from_id":"a","to_id":"y","predicate":"calls","asserted_by":"MANUAL"},
            {"id":4,"from_id":"y","to_id":"a","predicate":"called_by","asserted_by":"INVERSE_DERIVATION"}]
        self.assertEqual({e["id"] for e in R.owned_edges(rows,{"a"})},{1,2})

    def test_migration_roundtrip_and_atomic_conflict(self):
        from ..maintenance import cleanup4 as M
        c=sqlite3.connect(":memory:");c.row_factory=sqlite3.Row;DB.init_db(c)
        n={"id":"t","path":T.PREFIXES[0]+"raw/"+("a"*64)+".json",
           "type":"Config","family":"code","summary":"Config keys: observed_at",
           "metadata":"{}","content_hash":"fixed"}
        DB.upsert_node(c,{**n,"metadata":{}})
        c.commit()
        before={k:n[k] for k in M.FIELDS}
        after={**before,"type":"Event","family":"event","summary":"Machine telemetry: observed_at","metadata":json.dumps({"record_class":"machine_telemetry"})}
        p={"nodes":[{"id":"t","path":n["path"],"content_hash":"fixed","before":before,"after":after}],
           "code_ids":[],"edges_before":[],"edges_after":[]}
        p["plan_sha256"]=M.digest(p)
        with patch.object(M,"allowed",return_value=True):
            M.apply(c,p)
            self.assertEqual(c.execute("select type from nodes").fetchone()[0],"Event")
            self.assertEqual(c.execute("select summary from fts_nodes").fetchone()[0],after["summary"])
            M.apply(c,p,rollback=True)
            self.assertEqual(c.execute("select type from nodes").fetchone()[0],"Config")
            self.assertEqual(c.execute("select summary from fts_nodes").fetchone()[0],before["summary"])
            c.execute("update nodes set path='/changed'");c.commit()
            with self.assertRaisesRegex(ValueError,"concurrent node change"):M.apply(c,p)
            self.assertEqual(c.execute("select type from nodes").fetchone()[0],"Config")
        c.close()




class TelemetryTransport(unittest.TestCase):
    def test_query_and_mcp_explicit_evidence_mode(self):
        from .test_capacity_integration import CapacityIntegration
        from .. import brain as B,mcp_server as MCP
        fixture=CapacityIntegration()
        fixture.setUp()
        try:
            c=DB.connect()
            for nid,md in [("telemetryfixture",{"record_class":"machine_telemetry"}),
                           ("private_telemetryfixture",{"record_class":"machine_telemetry","capacity":"personal"})]:
                DB.upsert_node(c,{"id":nid,"path":str(fixture.business/(nid+".json")),
                    "type":"Event","family":"event","title":nid,"summary":"telemetryfixture",
                    "body_excerpt":"telemetryfixture","metadata":md,"status":"active"})
            c.commit();c.close()
            self.assertFalse(B.query("telemetryfixture").nodes)
            self.assertEqual({n["id"] for n in B.query("telemetryfixture",families=["event"]).nodes},{"telemetryfixture"})
            def search(include):
                return MCP.handle({"id":1,"method":"tools/call","params":{"name":"brain_search",
                    "arguments":{"text":"telemetryfixture","include_telemetry":include}}})
            self.assertEqual(json.loads(search(False)["result"]["content"][0]["text"]),[])
            yes=search(True)
            content=json.loads(yes["result"]["content"][0]["text"])
            self.assertEqual({n["id"] for n in content},{"telemetryfixture"})
            self.assertIn("error",search("yes"))
        finally:
            fixture.tearDown()

    def test_importer_refuses_wildcard_rebound_export(self):
        source=node("p/main.py","from helper import go\ngo()\n")
        target=node("p/helper.py","def go(): pass\nfrom external import *\n")
        self.assertEqual(sites(edges(source,target),"go")[0]["resolution"],"unresolved")


class WatcherSchema(unittest.TestCase):
    def test_wake_schema_and_real_config_boundary(self):
        record={"at":"2026-07-15","wake_score":1.0,"snapshot":{},"verdict":"observed"}
        path=T.PREFIXES[1]+"wakes/wake_PYPL_20260715-145505.json"
        self.assertEqual(T.classify(path,record)["classification_basis"],"collection-observation-schema")
        self.assertIsNone(T.classify(T.PREFIXES[1]+"config.json",{"wake_threshold":1.0,"enabled":True}))
        self.assertIsNone(T.classify("07 ⚙️ OPS/config/config.json",record))
        self.assertIsNone(T.classify(path,{"at":"2026-07-15","wake_score":1.0}))


class WatcherObservedSchemas(unittest.TestCase):
    def test_measured_wake_shapes(self):
        path=T.PREFIXES[1]+"wakes/wake_TEST_20260715-145505.json"
        for record in [
            {"fired":False,"reason":"fixture","score":1,"snapshot":{},"symbol":"TEST"},
            {"at":"2026-07-15","wake_score":1,"snapshot":{},"symbol":"TEST","status":"observed"},
        ]:
            self.assertIsNotNone(T.classify(path,record))
            self.assertIsNone(T.classify("07 ⚙️ OPS/config/config.json",record))
        self.assertIsNone(T.classify(T.PREFIXES[1]+"config.json",{"enabled":True,"wake_threshold":1}))
        self.assertIsNone(T.classify(T.PREFIXES[1]+"stats_cache.json",{"curves":[],"date":"fixture","window":3}))
        for record in [
            {"fired":False,"reason":"fixture","score":1,"snapshot":{},"symbol":"TEST"},
            {"at":"fixture","wake_score":1,"snapshot":{},"symbol":"TEST","status":"observed"},
        ]:
            self.assertIsNone(T.classify(T.PREFIXES[0]+"ordinary.json",record))
            incomplete=dict(record);incomplete.pop("snapshot")
            self.assertIsNone(T.classify(path,incomplete))
