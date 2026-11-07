"""Hermetic desk retrieval tests, including admission before desk payloads."""
import hashlib
import json
import re
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from .. import brain as B, capacity_admission as A, config as C, db as DB
from .. import http_server as HTTP, mcp_server as MCP, vega_index as V


@pytest.fixture
def desk(tmp_path, monkeypatch):
    monkeypatch.setenv("ARCTURION_VEGA_DESK", "1")
    monkeypatch.setattr(C, "BRAIN_DB", tmp_path / "brain.sqlite")
    monkeypatch.setattr(C, "DESKTOP_ROOT", tmp_path)
    monkeypatch.setattr(A, "STRICT_BUSINESS_UNION", True)
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps([
        {"id":"biz", "capacity":"business", "hq_admitted":True, "read_roots":[str(tmp_path / "VEGA" / "02 Strategies" / "biz")]},
        {"id":"other", "capacity":"business", "hq_admitted":True, "read_roots":[str(tmp_path / "VEGA" / "02 Strategies" / "other")]},
    ]))
    monkeypatch.setattr(A, "REGISTRY_PATH", registry)
    monkeypatch.setattr(A, "SHARED_SOURCES", ())
    monkeypatch.setattr(A, "BUSINESS_GOVERNING_SOURCES", ())
    conn = DB.connect(); DB.init_db(conn)
    for ddl in V.SCHEMA:
        conn.execute(ddl)
    conn.execute("INSERT INTO vega_desk_meta VALUES (?,?)", ("pack_sha256", hashlib.sha256(V.PACK_PATH.read_bytes()).hexdigest()))
    from .. import vega_corpus
    conn.executemany("INSERT INTO vega_desk_meta VALUES (?,?)", [
        ("corpus_pack_sha256", hashlib.sha256(V.PACK_PATH.read_bytes()).hexdigest()),
        ("corpus_version", str(vega_corpus.CORPUS_VERSION)),
    ])
    concepts = [
        ("orb", "Opening Range Breakout", "strategy", "trading", "", '["ORB"]'),
        ("opening-range", "Opening Range", "concept", "trading", "", '[]'),
        ("risk", "Risk Limit", "concept", "trading", "", '[]'),
    ]
    conn.executemany("INSERT INTO vega_desk_concepts VALUES (?,?,?,?,?,?)", concepts)
    conn.execute("INSERT INTO vega_desk_predicates VALUES (?,?,?,?)", ("requires", "requires", '[]', '[]'))
    for name, folder, status, digest_valid in [
        ("allowed", "biz", "accepted", True), ("other", "other", "accepted", True),
        ("stale", "biz", "accepted", False), ("queued", "biz", "queued", True),
        ("quarantine", "biz", "accepted", True), ("similar", "biz", "accepted", True),
    ]:
        path = "VEGA/02 Strategies/" + folder + "/" + name + ".md"
        quote = "Evidence for " + name
        source = tmp_path / path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(quote)
        digest = hashlib.sha256(quote.encode()).hexdigest()
        DB.upsert_node(conn, {"id": name, "type":"Source", "family":"document", "title":name,
            "path":path, "owner":"VEGA", "status":"active", "content_hash":digest,
            "summary":quote, "body_excerpt":quote, "metadata":{}})
        if name == "quarantine":
            conn.execute("INSERT INTO node_retrieval_quarantine(node_id,content_hash,reason,scope) VALUES (?,?,?,?)", (name,digest,"fixture","all"))
        conn.execute("INSERT INTO vega_desk_evidence VALUES (?,?,?,?,?,?,?)", (name,path,name,quote,0,len(quote),digest if digest_valid else "0" * 64))
        source_concept = "opening-range" if name == "similar" else "orb"
        conn.execute("INSERT INTO vega_desk_edges VALUES (?,?,?,?,?,?,?,?,?)", (name,source_concept,"risk","requires","rule",'{}',name,status,"fixture"))
    conn.commit(); conn.close()
    return tmp_path


def test_scoped_aliases_ranking_and_locators(desk):
    result = B.query("What is the ORB strategy?", desk="VEGA", project_id="biz")
    data = result.to_dict()
    assert [x["id"] for x in data["claims"]] == ["allowed"]
    evidence = data["claims"][0]["evidence"]
    assert evidence["path"].endswith("biz/allowed.md")
    assert evidence["start_offset"] == 0 and evidence["end_exclusive"] is True
    assert evidence["source_hash"] == hashlib.sha256(b"Evidence for allowed").hexdigest()
    assert "allowed.md" in result.text_bundle and "Unicode offsets" in result.text_bundle
    link = re.search(r"\[source\]\(<([^>]+)>\)", result.text_bundle)
    assert link and Path(link.group(1)).is_file()
    assert "Evidence for other" not in str(data)
    assert "Evidence for stale" not in str(data)
    assert "Evidence for queued" not in str(data)
    assert "Evidence for quarantine" not in str(data)
    disambiguated = B.query("opening range breakout", desk="VEGA", project_id="biz").to_dict()["claims"]
    assert disambiguated[0]["from"]["id"] == "orb"
    assert B.query("unknownxylophone", desk="VEGA", project_id="biz").to_dict()["claims"] == []
    assert not B.query("ORB", desk="VEGA", project_id="biz", predicates=["other"]).claims


def test_disabled_unknown_and_unsupported_fail_closed(desk, monkeypatch):
    monkeypatch.setenv("ARCTURION_VEGA_DESK", "0")
    with pytest.raises(ValueError, match="disabled"):
        B.query("ORB", desk="VEGA")
    monkeypatch.setenv("ARCTURION_VEGA_DESK", "1")
    with pytest.raises(ValueError, match="unknown desk"):
        B.query("ORB", desk="OTHER")
    with pytest.raises(ValueError, match="supports"):
        B.query("ORB", desk="VEGA", families=["document"])
    with pytest.raises(ValueError, match="no admitted"):
        B.query("ORB", desk="VEGA", project_id="nonexistent")


def test_stale_source_and_pack_changes_fail_closed(desk, monkeypatch, tmp_path):
    (desk / "VEGA/02 Strategies/biz/allowed.md").write_text("changed after indexing")
    assert not B.query("ORB", desk="VEGA", project_id="biz").claims
    from .. import vega_query as Q
    changed_pack = tmp_path / "changed.json"
    changed_pack.write_text("{}")
    monkeypatch.setattr(Q, "PACK_PATH", changed_pack)
    with pytest.raises(ValueError, match="pack changed"):
        B.query("ORB", desk="VEGA", project_id="biz")


@pytest.mark.parametrize("field,value", [("status", "withdrawn"), ("status", "superseded"),
                                         ("status", "expired"), ("valid_until", "2000-01-01"),
                                         ("valid_from", "2999-01-01"), ("valid_until", "invalid")])
def test_lifecycle_exclusions(desk, field, value):
    conn = DB.connect()
    conn.execute(f"UPDATE nodes SET {field}=? WHERE id='allowed'", (value,))
    conn.commit(); conn.close()
    assert not B.query("ORB", desk="VEGA", project_id="biz").claims


def test_retired_source_and_pending_review_are_visible(desk):
    registry = desk / "registry.json"
    registry.write_text(json.dumps([{"id":"biz", "capacity":"business", "hq_admitted":True,
                                     "read_roots":[str(desk / "ATLAS")]}]))
    path = "ATLAS/source.md"
    content = "Evidence for allowed\n> sync_status: pending_review\n> VALIDATION FAILED — RETIRED\n"
    p = desk / path; p.parent.mkdir(parents=True); p.write_text(content)
    digest = hashlib.sha256(content.encode()).hexdigest()
    conn = DB.connect()
    conn.execute("UPDATE nodes SET path=?,content_hash=? WHERE id='allowed'", (path,digest))
    conn.execute("UPDATE vega_desk_evidence SET source_path=?,source_hash=? WHERE id='allowed'", (path,digest))
    conn.commit(); conn.close()
    pack = B.query("historical ORB", desk="VEGA", project_id="biz")
    assert pack.claims[0]["evidence"]["source_review"] == "pending_review"
    assert pack.claims[0]["evidence"]["source_retired"] is True
    assert "projection pending review" in pack.text_bundle and "RETIRED historical" in pack.text_bundle
    assert not B.query("active ORB", desk="VEGA", project_id="biz").claims


def test_admission_and_claim_share_wal_snapshot(desk, monkeypatch):
    from .. import vega_query as Q
    original = Q.filter_retrieval
    def concurrent_update(conn, rows):
        admitted = original(conn, rows)
        writer = DB.connect()
        writer.execute("UPDATE nodes SET metadata=? WHERE id='allowed'", ('{\"capacity\":\"personal\"}',))
        writer.execute("UPDATE vega_desk_edges SET status='rejected' WHERE id='allowed'")
        writer.commit(); writer.close()
        return admitted
    monkeypatch.setattr(Q, "filter_retrieval", concurrent_update)
    first = B.query("ORB", desk="VEGA", project_id="biz")
    assert [c["id"] for c in first.claims] == ["allowed"]
    monkeypatch.setattr(Q, "filter_retrieval", original)
    assert not B.query("ORB", desk="VEGA", project_id="biz").claims


def test_directed_paths_scope_review_and_rollback(desk):
    conn = DB.connect()
    conn.executemany("INSERT INTO vega_desk_concepts VALUES (?,?,?,?,?,?)", [
        ("specialist", "Specialist process", "process", "risk", "", "[]"),
        ("middle", "Intermediate measure", "metric", "risk", "", "[]"),
        ("destination", "Risk boundary", "risk", "risk", "", "[]"),
    ])
    conn.executemany("INSERT INTO vega_desk_predicates VALUES (?,?,?,?)", [
        ("first", "uses", "[]", "[]"), ("second", "is bounded by", "[]", "[]")])
    conn.executemany("INSERT INTO vega_desk_edges VALUES (?,?,?,?,?,?,?,?,?)", [
        ("hop1", "specialist", "middle", "first", "rule", '{"window":"day"}', "allowed", "accepted", "first"),
        ("hop2", "middle", "destination", "second", "hypothesis", '{"window":"week"}', "similar", "accepted", "second"),
    ])
    conn.commit(); conn.close()
    direct = B.query("specialist", desk="VEGA", project_id="biz", predicates=["first"]).to_dict()
    assert direct["answer_status"] == "direct_evidence"
    result = B.query("specialist", desk="VEGA", project_id="biz", predicates=["first"], desk_hops=2).to_dict()
    assert result["answer_status"] == "sourced_paths"
    assert len(result["paths"]) == 1 and result["paths"][0]["hop_count"] == 2
    hops = result["paths"][0]["hops"]
    assert [h["id"] for h in hops] == ["hop1", "hop2"]
    assert hops[0]["evidence"]["source_node_id"] != hops[1]["evidence"]["source_node_id"]
    assert [h["context"]["window"] for h in hops] == ["day", "week"]
    assert "does not prove causation or performance" in result["text_bundle"]
    assert {h["id"] for h in hops} <= {e["id"] for e in result["edges"]}
    # Tight budget must never leave a path referring to an omitted edge.
    bounded = B.query("specialist", desk="VEGA", project_id="biz", desk_hops=2, token_budget=100).to_dict()
    assert all(h["id"] in {e["id"] for e in bounded["edges"]} for p in bounded["paths"] for h in p["hops"])
    conn = DB.connect()
    conn.execute("UPDATE nodes SET metadata=? WHERE id='similar'", ('{\"capacity\":\"personal\"}',))
    conn.commit(); conn.close()
    restricted = B.query("specialist", desk="VEGA", project_id="biz", desk_hops=2).to_dict()
    assert [h["id"] for h in restricted["paths"][0]["hops"]] == ["hop1"]
    assert "Evidence for similar" not in str(restricted)
    conn = DB.connect()
    conn.execute("UPDATE nodes SET metadata='{}' WHERE id='similar'")
    conn.execute("UPDATE vega_desk_edges SET status='queued' WHERE id='hop2'")
    conn.commit(); conn.close()
    assert B.query("specialist", desk="VEGA", project_id="biz", desk_hops=2).paths[0]["hop_count"] == 1
    conn = DB.connect()
    conn.execute("UPDATE vega_desk_edges SET status='accepted' WHERE id='hop2'")
    conn.execute("UPDATE nodes SET status='retired' WHERE id='similar'")
    conn.commit(); conn.close()
    assert B.query("specialist", desk="VEGA", project_id="biz", desk_hops=2).paths[0]["hop_count"] == 1
    conn = DB.connect()
    conn.execute("UPDATE nodes SET status='active' WHERE id='similar'")
    conn.commit(); conn.close()
    assert B.query("specialist", desk="VEGA", project_id="biz", desk_hops=2).paths[0]["hop_count"] == 2
    server = ThreadingHTTPServer(("127.0.0.1", 0), HTTP.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f"http://127.0.0.1:{server.server_port}/query"
    try:
        with urlopen(base + "?topic=specialist&desk=VEGA&project_id=biz&desk_hops=2&format=json") as response:
            assert json.load(response)["paths"][0]["hop_count"] == 2
        request = Request(base, data=json.dumps({"topic":"specialist", "desk":"VEGA", "project_id":"biz", "desk_hops":2}).encode(), headers={"Content-Type":"application/json"})
        with urlopen(request) as response:
            assert json.load(response)["paths"][0]["hop_count"] == 2
    finally:
        server.shutdown(); server.server_close(); thread.join()
    mcp = MCP.handle({"jsonrpc":"2.0", "id":2, "method":"tools/call", "params":{
        "name":"brain_query", "arguments":{"topic":"specialist", "desk":"VEGA", "project_id":"biz", "desk_hops":2, "format":"json"}}})
    assert json.loads(mcp["result"]["content"][0]["text"])["paths"][0]["hop_count"] == 2
    conn = DB.connect()
    for n in range(12):
        conn.execute("INSERT INTO vega_desk_concepts VALUES (?,?,?,?,?,?)", (f"leaf{n}",f"Leaf {n}","risk","risk","","[]"))
        conn.execute("INSERT INTO vega_desk_edges VALUES (?,?,?,?,?,?,?,?,?)", (f"branch{n}","middle",f"leaf{n}","second","rule",'{}',"similar","accepted","branch"))
    conn.commit(); conn.close()
    capped = B.query("specialist", desk="VEGA", project_id="biz", desk_hops=2, token_budget=8000).to_dict()
    assert 0 < len(capped["paths"]) <= 12 and capped["truncated"]
    assert any("capped at 12" in warning for warning in capped["warnings"])
    mcp_capped = MCP.handle({"jsonrpc":"2.0", "id":3, "method":"tools/call", "params":{
        "name":"brain_query", "arguments":{"topic":"specialist", "desk":"VEGA", "project_id":"biz", "desk_hops":2, "budget":8000, "format":"json"}}})
    assert json.loads(mcp_capped["result"]["content"][0]["text"])["paths"]
    with pytest.raises(ValueError, match="desk_hops"):
        B.query("specialist", desk="VEGA", desk_hops=4)
    with pytest.raises(ValueError, match="requires"):
        B.query("specialist", desk_hops=2)


def test_large_desk_checks_freshness_only_for_matching_sources(desk, monkeypatch):
    conn = DB.connect()
    conn.execute("INSERT INTO vega_desk_claim_origin VALUES (?,?)", ("allowed", "seed"))
    for n in range(2500):
        name = f"bulk{n}"
        path = f"VEGA/02 Strategies/biz/{name}.md"
        digest = hashlib.sha256(name.encode()).hexdigest()
        DB.upsert_node(conn, {"id":name, "type":"Source", "family":"document", "title":name,
            "path":path, "status":"active", "content_hash":digest, "metadata":{}})
        conn.execute("INSERT INTO vega_desk_concepts VALUES (?,?,?,?,?,?)", (name,name,"document","catalog","","[]"))
        conn.execute("INSERT INTO vega_desk_evidence VALUES (?,?,?,?,?,?,?)", (name,path,name,name,0,len(name),digest))
        conn.execute("INSERT INTO vega_desk_edges VALUES (?,?,?,?,?,?,?,?,?)", (name,name,"risk","requires","catalog",'{}',name,"accepted",name))
        conn.execute("INSERT INTO vega_desk_claim_origin VALUES (?,?)", (name,"corpus"))
    conn.commit(); conn.close()
    opened = []
    original = Path.read_bytes
    def counted(path):
        opened.append(str(path))
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", counted)
    start = time.perf_counter()
    result = B.query("ORB", desk="VEGA", project_id="biz")
    elapsed = time.perf_counter() - start
    print(f"2500 extra indexed sources: query {elapsed:.3f}s; unrelated source files opened: {sum('bulk' in path for path in opened)}")
    assert result.claims[0]["id"] == "allowed"
    assert not any("bulk" in path for path in opened)
    opened.clear()
    path_result = B.query("orb", desk="VEGA", project_id="biz", desk_hops=2)
    assert path_result.paths and not any("bulk" in path for path in opened)


def test_single_hop_mcp_preserves_primary_evidence_with_large_catalog(desk):
    conn = DB.connect()
    conn.execute("INSERT INTO vega_desk_claim_origin VALUES (?,?)", ("allowed", "seed"))
    conn.execute("INSERT INTO vega_desk_concepts VALUES (?,?,?,?,?,?)", ("catalog_orb", "ORB", "document", "catalog", "", "[]"))
    for number in range(80):
        eid = f"catalog_{number}"
        context = json.dumps({"source_note": "Historical source 📈 with Unicode evidence. " * 3})
        conn.execute("INSERT INTO vega_desk_edges VALUES (?,?,?,?,?,?,?,?,?)",
                     (eid, "catalog_orb", "risk", "requires", "catalog", context, "allowed", "accepted", eid))
        conn.execute("INSERT INTO vega_desk_claim_origin VALUES (?,?)", (eid, "corpus"))
    conn.commit()
    conn.close()
    for budget in (None, 1600, 8000):
        args = {"topic": "ORB", "desk": "VEGA", "project_id": "biz", "format": "json"}
        if budget is not None:
            args["budget"] = budget
        rpc = MCP.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                          "params": {"name": "brain_query", "arguments": args}})
        text = rpc["result"]["content"][0]["text"]
        assert len(text.encode("utf-8")) <= MCP.RESPONSE_CAP_BYTES
        result = json.loads(text)
        assert result["claims"][0]["id"] == "allowed" and result["truncated"]
        assert result["edges"][0]["evidence"] == result["claims"][0]["evidence"]


def test_catalog_is_labeled_and_cannot_crowd_out_seed(desk):
    from .. import vega_query as Q
    conn = DB.connect()
    conn.execute("INSERT INTO vega_desk_claim_origin VALUES (?,?)", ("allowed", "seed"))
    conn.execute("INSERT INTO vega_desk_concepts VALUES (?,?,?,?,?,?)", ("catalog_orb","ORB","document","catalog","",'[]'))
    conn.execute("INSERT INTO vega_desk_edges VALUES (?,?,?,?,?,?,?,?,?)", ("aaa_catalog","catalog_orb","risk","requires","catalog",'{}',"allowed","accepted","catalog"))
    conn.execute("INSERT INTO vega_desk_claim_origin VALUES (?,?)", ("aaa_catalog", "corpus"))
    conn.commit(); conn.close()
    result = B.query("ORB", desk="VEGA", project_id="biz", predicates=["requires"]).to_dict()
    assert result["claims"][0]["id"] == "allowed"
    catalog = next(claim for claim in result["claims"] if claim["id"] == "aaa_catalog")
    assert catalog["answer_role"] == "catalog_navigation"
    assert "Catalog navigation only" in result["text_bundle"]
    ordinary = B.query("ORB", desk="VEGA", project_id="biz").to_dict()
    assert all(claim["kind"] != "catalog" for claim in ordinary["claims"])
    coverage = Q.coverage_stats(project_id="biz")
    assert coverage["query_visible_corpus_sources"] == 1 and coverage["query_visible_corpus_edges"] == 1
    assert coverage["query_visible_navigation_sources"] == 1 and coverage["query_visible_navigation_assertions"] == 1
    assert coverage["query_visible_substantive_sources"] >= 1 and coverage["query_visible_substantive_assertions"] >= 1
    scoped = B.scoped_connection("business", "biz")
    try:
        assert Q.coverage_stats(conn=scoped)["query_visible_corpus_sources"] == 1
    finally:
        scoped.close()
    unscoped = DB.connect()
    try:
        with pytest.raises(ValueError, match="scoped"):
            Q.coverage_stats(conn=unscoped)
    finally:
        unscoped.close()
    (desk / "VEGA/02 Strategies/biz/allowed.md").write_text("changed")
    assert Q.coverage_stats(project_id="biz")["query_visible_corpus_sources"] == 0


def test_specific_subjects_outrank_generic_seed_and_comparisons_keep_both(desk):
    conn = DB.connect()
    concepts = [
        ("option_delta", "Option delta"), ("volume_delta_fixture", "Volume delta"),
        ("funding", "Funding"), ("proxy", "Perpetual-versus-spot basis proxy"),
        ("rate", "Crypto funding rate"), ("generic_scan", "Long-term opportunity scan"),
        ("total", "Long-term total return"), ("growth", "Growth"),
        ("valuation", "Valuation"), ("yield", "Yield"),
    ]
    conn.executemany("INSERT INTO vega_desk_concepts VALUES (?,?,?,?,?,?)", [(ident,label,"metric","fixture","",'[]') for ident,label in concepts])
    entries = [
        ("opt", "option_delta", "risk", "seed"), ("vol", "volume_delta_fixture", "risk", "seed"),
        ("generic_funding", "funding", "risk", "seed"), ("exact_proxy", "proxy", "rate", "corpus"),
        ("generic_longterm", "generic_scan", "risk", "seed"),
        ("return_growth", "total", "growth", "corpus"),
        ("return_valuation", "total", "valuation", "corpus"),
        ("return_yield", "total", "yield", "corpus"),
    ]
    for ident, a, z, origin in entries:
        conn.execute("INSERT INTO vega_desk_edges VALUES (?,?,?,?,?,?,?,?,?)", (ident,a,z,"requires","definition",'{}',"allowed","accepted",ident))
        conn.execute("INSERT INTO vega_desk_claim_origin VALUES (?,?)", (ident,origin))
    conn.commit(); conn.close()
    comparison = B.query("How does option delta differ from volume delta?", desk="VEGA", project_id="biz", token_budget=1600)
    assert {"opt", "vol"} <= {claim["id"] for claim in comparison.claims}
    proxy = B.query("How does the perpetual-versus-spot basis proxy relate to the crypto funding rate?", desk="VEGA", project_id="biz", token_budget=1600)
    assert proxy.claims[0]["id"] == "exact_proxy"
    returns = B.query("Which components make up long-term total return?", desk="VEGA", project_id="biz", token_budget=1600)
    assert {"return_growth", "return_valuation", "return_yield"} <= {claim["id"] for claim in returns.claims}
    assert returns.claims[0]["id"].startswith("return_")


def test_subject_relation_and_object_grounding_beyond_orb(desk):
    conn = DB.connect()
    concepts = [("ibs", "IBS Reversion v1"), ("disaster", "Disaster stop"), ("entry", "Entry trigger"),
                ("mstr", "MSTR VWAP reclaim"), ("oos", "Out-of-sample validation failure"),
                ("proxy2", "Perpetual-versus-spot basis proxy"), ("fundrate", "Crypto funding rate"),
                ("total2", "Long-term total return"), ("growth2", "Earnings growth"),
                ("discount", "Valuation discount rate"), ("terminal", "Terminal growth rate")]
    conn.executemany("INSERT INTO vega_desk_concepts VALUES (?,?,?,?,?,?)", [(ident,label,"concept","fixture","",'[]') for ident,label in concepts])
    predicates = [("vega_places_stop_at", "places stop at"), ("vega_failed_validation_in", "failed validation"),
                  ("vega_corpus_proxies_for", "is a proxy for"), ("vega_corpus_has_return_component", "has return component"),
                  ("vega_corpus_must_exceed", "must exceed")]
    conn.executemany("INSERT INTO vega_desk_predicates VALUES (?,?,?,?)", [(p,l,'[]','[]') for p,l in predicates])
    rules = [("ibs_entry","ibs","entry","requires"), ("ibs_stop","ibs","disaster","vega_places_stop_at"),
             ("mstr_entry","mstr","entry","requires"), ("mstr_validation","mstr","oos","vega_failed_validation_in"),
             ("proxy_rule","proxy2","fundrate","vega_corpus_proxies_for"),
             ("return_rule","total2","growth2","vega_corpus_has_return_component"),
             ("inequality_rule","discount","terminal","vega_corpus_must_exceed")]
    for ident, a, z, pred in rules:
        conn.execute("INSERT INTO vega_desk_edges VALUES (?,?,?,?,?,?,?,?,?)", (ident,a,z,pred,"definition",'{}',"allowed","accepted",ident))
    conn.commit(); conn.close()
    for question, expected in [
        ("Where does IBS Reversion v1 place its disaster stop?", "ibs_stop"),
        ("What happened in out-of-sample validation for MSTR VWAP reclaim?", "mstr_validation"),
        ("How does the perpetual-versus-spot basis proxy relate to the crypto funding rate?", "proxy_rule"),
        ("Which components make up long-term total return?", "return_rule"),
        ("How must the discount rate compare with terminal growth in the valuation rule?", "inequality_rule"),
    ]:
        pack = B.query(question, desk="VEGA", project_id="biz", token_budget=1600)
        assert pack.claims[0]["id"] == expected
        assert pack.answer_status == "direct_evidence"
    ungrounded = B.query("Where does IBS Reversion v1 place its disaster stop at the broker?", desk="VEGA", project_id="biz")
    assert ungrounded.answer_status == "related_only" and "broker" in ungrounded.unmatched_terms


def test_desk_sources_return_only_locator_fields(desk):
    conn = DB.connect()
    private = "synthetic-field-excluded-from-the-cited-passage"
    conn.execute("UPDATE nodes SET summary=?, body_excerpt=?, metadata=? WHERE id='allowed'",
                 (private, private, json.dumps({"subject": private, "ownership_source": private})))
    conn.commit()
    conn.close()
    pack = B.query("ORB", desk="VEGA", project_id="biz")
    assert pack.claims[0]["evidence"]["quote"] == "Evidence for allowed"
    assert set(pack.nodes[0]) == {"id", "path", "content_hash", "prong"}
    assert private not in json.dumps(pack.to_dict())


def test_reviewed_corpus_hypothesis_is_substantive_and_stays_scoped(desk):
    from .. import vega_query as Q
    baseline = Q.coverage_stats(project_id="biz")
    conn = DB.connect()
    conn.executemany("INSERT INTO vega_desk_edges VALUES (?,?,?,?,?,?,?,?,?)", [
        ("catalog_link", "orb", "risk", "requires", "catalog", "{}", "allowed", "accepted", "c"),
        ("reviewed_claim", "orb", "risk", "requires", "hypothesis", "{}", "allowed", "accepted", "r"),
        ("outside_claim", "orb", "risk", "requires", "hypothesis", "{}", "other", "accepted", "o"),
        ("queued_claim", "orb", "risk", "requires", "hypothesis", "{}", "allowed", "queued", "q"),
    ])
    conn.executemany("INSERT INTO vega_desk_claim_origin VALUES (?,?)", [
        (edge_id, "corpus") for edge_id in ("catalog_link", "reviewed_claim", "outside_claim", "queued_claim")
    ])
    conn.commit()
    conn.close()
    result = B.query("orb", desk="VEGA", project_id="biz", predicates=["requires"]).to_dict()
    claims = {c["id"]: c for c in result["claims"]}
    assert claims["reviewed_claim"]["origin"] == "corpus"
    assert claims["reviewed_claim"]["kind"] == "hypothesis"
    assert claims["reviewed_claim"]["answer_role"] == "direct_evidence"
    assert claims["catalog_link"]["answer_role"] == "catalog_navigation"
    assert "queued_claim" not in claims and "outside_claim" not in claims
    ids = [c["id"] for c in result["claims"]]
    assert ids.index("reviewed_claim") < ids.index("catalog_link")
    counts = Q.coverage_stats(project_id="biz")
    assert counts["query_visible_navigation_assertions"] == baseline["query_visible_navigation_assertions"] + 1
    assert counts["query_visible_substantive_assertions"] == baseline["query_visible_substantive_assertions"] + 1
    assert counts["query_visible_corpus_edges"] == baseline["query_visible_corpus_edges"] + 2


@pytest.mark.parametrize("meta_key", ["corpus_version", "corpus_pack_sha256"])
def test_stale_corpus_policy_withholds_only_corpus(desk, meta_key):
    from .. import vega_query as Q
    conn = DB.connect()
    conn.execute("INSERT INTO vega_desk_claim_origin VALUES (?,?)", ("allowed", "corpus"))
    conn.execute("UPDATE vega_desk_meta SET value='old' WHERE key=?", (meta_key,))
    conn.commit()
    conn.close()
    assert not B.query("ORB", desk="VEGA", project_id="biz").claims
    assert B.query("Opening Range", desk="VEGA", project_id="biz").claims
    assert Q.coverage_stats(project_id="biz")["query_visible_corpus_edges"] == 0


@pytest.mark.parametrize("origin", ["corpus", "declaration", "literal"])
def test_protected_workspace_path_is_withheld_before_file_read(desk, monkeypatch, origin):
    from .. import vega_query as Q
    conn = DB.connect()
    old = "VEGA/02 Strategies/biz/allowed.md"
    private = "VEGA/02 Strategies/biz/Positions/allowed.md"
    conn.execute("INSERT INTO vega_desk_claim_origin VALUES (?,?)", ("allowed", origin))
    conn.execute("UPDATE nodes SET path=? WHERE id='allowed'", (private,))
    conn.execute("UPDATE vega_desk_evidence SET source_path=? WHERE id='allowed'", (private,))
    conn.commit()
    conn.close()
    dest = desk / private
    dest.parent.mkdir()
    (desk / old).rename(dest)
    original = Path.read_bytes
    def checked(path):
        assert path != dest, "protected corpus body was opened"
        return original(path)
    monkeypatch.setattr(Path, "read_bytes", checked)
    assert not B.query("ORB", desk="VEGA", project_id="biz").claims
    assert Q.coverage_stats(project_id="biz")["query_visible_corpus_edges"] == 0


def test_intents_and_unknowns(desk):
    conn = DB.connect()
    for predicate in ("vega_triggers_on", "vega_requires_confirmation_from", "vega_places_stop_at",
                      "vega_invalidated_by", "vega_stands_aside_when"):
        conn.execute("INSERT INTO vega_desk_predicates VALUES (?,?,?,?)", (predicate,predicate,'[]','[]'))
        conn.execute("INSERT INTO vega_desk_edges VALUES (?,?,?,?,?,?,?,?,?)", (predicate,"orb","risk",predicate,"rule",'{}',"allowed","accepted","fixture"))
    conn.commit(); conn.close()
    for question, predicate in [
        ("What triggers the ORB?", "vega_triggers_on"),
        ("What confirms the ORB?", "vega_requires_confirmation_from"),
        ("Where is the ORB stop?", "vega_places_stop_at"),
        ("What kills the ORB?", "vega_invalidated_by"),
        ("When does the ORB stand aside?", "vega_stands_aside_when"),
        ("After ORB entry where are the stops?", "vega_places_stop_at"),
        ("How is an ORB breakout confirmed?", "vega_requires_confirmation_from"),
        ("When is an ORB a failure?", "vega_invalidated_by"),
        ("When is an ORB a no trade?", "vega_stands_aside_when"),
    ]:
        assert [x["predicate"] for x in B.query(question, desk="VEGA", project_id="biz").edges] == [predicate]
    direct = B.query("Where is the ORB stop?", desk="VEGA", project_id="biz").to_dict()
    assert direct["answer_status"] == "direct_evidence"
    for question in ("Which broker executes ORB?", "What venue routes ORB orders?",
                     "Who owns the ORB copyright?", "Does ORB include holiday sessions?",
                     "Where is the ORB stop on a holiday?"):
        result = B.query(question, desk="VEGA", project_id="biz").to_dict()
        assert result["claims"] and result["answer_status"] == "related_only"
        assert all(claim["answer_role"] == "related_evidence_only" for claim in result["claims"])
        assert all(edge["answer_role"] == "related_evidence_only" for edge in result["edges"])
        assert "Related evidence only" in result["text_bundle"]
    for question in ("What is the ORB win rate next year?", "What will tomorrow bring?",
                     "What was my last copper roll fill?", "What is the exact slippage?",
                     "Is this unnamed hedge documented?", "What is the price next month?",
                     "Who will win this contract?", "Is the ORB profitable?",
                     "What average return does the ORB produce?",
                     "What exact percentage commission applies to ORB?",
                     "What is the volume threshold in contracts for this ORB?"):
        assert not B.query(question, desk="VEGA", project_id="biz").claims


def test_http_and_mcp_adapter(desk):
    server = ThreadingHTTPServer(("127.0.0.1", 0), HTTP.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f"http://127.0.0.1:{server.server_port}/query"
    try:
        with urlopen(base + "?topic=ORB&desk=VEGA&project_id=biz&format=json") as response:
            assert json.load(response)["claims"][0]["id"] == "allowed"
        request = Request(base, data=json.dumps({"topic":"ORB","desk":"VEGA","project_id":"biz"}).encode(), headers={"Content-Type":"application/json"})
        with urlopen(request) as response:
            assert json.load(response)["claims"][0]["id"] == "allowed"
        with pytest.raises(HTTPError) as err:
            urlopen(base + "?topic=ORB&desk=OTHER")
        assert err.value.code == 400
    finally:
        server.shutdown(); server.server_close(); thread.join()
    schema = next(t for t in MCP.TOOLS if t["name"] == "brain_query")["inputSchema"]
    assert "desk" in schema["properties"]
    result = MCP.handle({"jsonrpc":"2.0", "id":1, "method":"tools/call", "params":{
        "name":"brain_query", "arguments":{"topic":"ORB", "desk":"VEGA", "project_id":"biz", "format":"json"}}})
    assert json.loads(result["result"]["content"][0]["text"])["claims"][0]["id"] == "allowed"
