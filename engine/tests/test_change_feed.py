"""Hermetic staged change-feed tests; never opens the live index."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch
from engine import db as DB, config as C, capacity_admission as CA
from engine import change_feed as F

class BrainChangesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.path=self.root/"brain.sqlite"
        self.conn=DB.connect(self.path);DB.init_db(self.conn)
        self.p1=patch.object(C,"BRAIN_DB",self.path);self.p1.start()
        self.policy=CA.AdmissionPolicy("business",(str(self.root/"business"),),(),())
        self.personal=CA.AdmissionPolicy("personal",(str(self.root/"personal"),),(),())
        self.p2=patch.object(CA,"load_admission",side_effect=lambda capacity,project_id=None:self.personal if capacity=="personal" else self.policy);self.p2.start()
        from types import SimpleNamespace
        self.p3=patch("engine.memory_policy.load_policy",return_value=SimpleNamespace(data={},allows=lambda node,resolve:True));self.p3.start()
    def tearDown(self):
        self.p3.stop();self.p2.stop();self.p1.stop();self.conn.close();self.tmp.cleanup()
    def node(self,ref="one",world="business",digest="a",stamp="2026-11-05T19:00:00Z",**extra):
        node=dict(id=ref,type="Document",family="document",title="Project "+ref,
                  path=str(self.root/world/(ref+".md")),owner="ALTAIR",status="active",
                  content_hash=digest,body_excerpt="PRIVATE BODY SENTINEL",summary="PRIVATE SUMMARY SENTINEL",
                  last_modified=stamp,metadata={"capacity":world})
        node.update(extra);DB.upsert_node(self.conn,node);self.conn.commit()
    def test_not_installed_fails_closed_and_install_is_explicit_idempotent(self):
        self.node()
        with self.assertRaises(ValueError):F.changes()
        F.install(self.conn);self.conn.commit()
        epoch=self.conn.execute("SELECT epoch FROM change_feed_config").fetchone()[0]
        first=F.changes()
        F.install(self.conn);self.conn.commit()
        self.assertEqual(epoch,self.conn.execute("SELECT epoch FROM change_feed_config").fetchone()[0])
        self.assertEqual(len(first["records"]),1)
        self.assertTrue(first["records"][0]["historical"])
        self.assertEqual(len(F.changes()["records"]),1)
    def test_monotonic_cursor_catches_backdated_same_time_edits_and_restart(self):
        F.install(self.conn);self.conn.commit();self.node()
        first=F.changes(limit=1)
        self.node(digest="b",stamp="2020-01-01T00:00:00Z")
        self.node(digest="c",stamp="2020-01-01T00:00:00Z")
        later=F.changes(cursor=first["next_cursor"],limit=1)
        self.assertEqual(len(later["records"]),1)
        self.assertEqual(later["records"][0]["content_hash"],"c")
        self.assertFalse(later["records"][0]["historical"])
        self.assertEqual(F.changes(cursor=later["next_cursor"])["records"],[])
    def test_admission_privacy_and_deleted_nodes_never_project_payload(self):
        F.install(self.conn);self.conn.commit()
        self.node("business");self.node("personal","personal")
        self.node("contrary",metadata={"capacity":"personal"})
        self.node("quarantined",digest="quarantine")
        self.conn.execute("INSERT INTO node_retrieval_quarantine(node_id,content_hash,reason,scope) VALUES(?,?,?,?)",
                          ("quarantined","quarantine","test","all"))
        self.conn.commit()
        rows=F.changes()
        self.assertEqual([r["id"] for r in rows["records"]],["business"])
        self.assertNotIn("SENTINEL",json.dumps(rows))
        self.conn.execute("DELETE FROM nodes WHERE id='business'");self.conn.commit()
        self.assertEqual(F.changes(cursor=rows["next_cursor"])["records"],[])
    def test_excluded_records_advance_durable_cursor_and_scope_epoch_mismatch_rejected(self):
        F.install(self.conn);self.conn.commit()
        for i in range(5):self.node("private"+str(i),"personal")
        self.node("allowed")
        first=F.changes(limit=1)
        self.assertEqual(first["records"][0]["id"],"allowed")
        self.assertFalse(first["has_more"])
        with self.assertRaises(ValueError):F.changes(capacity="personal",cursor=first["next_cursor"])
        self.conn.execute("UPDATE change_feed_config SET epoch='new-generation'");self.conn.commit()
        with self.assertRaises(ValueError):F.changes(cursor=first["next_cursor"])
    def test_route_parameters_and_malformed_cursors_are_bounded(self):
        F.install(self.conn);self.conn.commit()
        for value in ("bad","x"*3000):
            with self.assertRaises(ValueError):F.changes(cursor=value)
        with self.assertRaises(ValueError):F.changes(limit=201)
        with self.assertRaises(ValueError):F.http_response({"include_telemetry":["true"]})
        result=F.http_response({"capacity":["business"],"limit":["1"]})
        self.assertEqual(result["schema"],"brain.changes.v1")
    def test_superseded_same_hash_metadata_and_delete_reinsert_are_not_misrepresented(self):
        F.install(self.conn);self.conn.commit()
        self.node(digest="same",status="draft")
        self.node(digest="same",status="active")
        rows=F.changes()["records"]
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["status"],"active")
        self.conn.execute("DELETE FROM nodes WHERE id='one'");self.conn.commit()
        self.node(digest="same",status="active")
        rows=F.changes()["records"]
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]["operation"],"insert")
if __name__=="__main__":unittest.main()

