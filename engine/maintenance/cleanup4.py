"""Scoped, compare-before-write cleanup migration. Default is plan, never apply.

Stores only affected metadata and graph rows; does not copy source bodies, crawl
ATLAS or invoke the full reindex/GC/backup pipeline. Rollback checks after-images.
"""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from .. import config as C, code_references as R, telemetry as T
from ..source_integrity import source_path
from ..util import content_hash, should_skip_path

FIELDS = ("type","family","summary","metadata")
ALLOWED_ROOTS = {C.UNIVERSE_ROOT.name,C.BRAIN_ROOT.name,C.RUNTIME_ROOT.name,*C.CONSTELLATION_AGENTS}
def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def allowed(path):
    return R.allowed_source(path)

def plan(conn, exclude_paths=()):
    conn.row_factory=sqlite3.Row
    nodes=[];catalog={};code_nodes=[];skips={};schema_checked=0
    for row in conn.execute("SELECT id,path,type,family,summary,metadata,content_hash FROM nodes WHERE path LIKE '%.py' OR (type='Config' AND path LIKE '07 ⚙️ OPS/state/%')"):
        old=dict(row);path=old["path"]
        if not allowed(path) or path in exclude_paths: continue
        md=R._loads(old["metadata"])
        updated={key:old[key] for key in FIELDS}
        if path.endswith(".py"):
            p=source_path(path)
            try:
                raw=p.read_bytes()
                if content_hash(raw)!=old["content_hash"]:
                    skips["changed_source"]=skips.get("changed_source",0)+1;continue
                analysis=R.analyze(raw.decode("utf-8",errors="replace"),path)
                if analysis.get("parse_error"):
                    skips["syntax_error"]=skips.get("syntax_error",0)+1;continue
                md["python_analysis"]=analysis
                updated["metadata"]=json.dumps(md,ensure_ascii=False)
                new={**old,**updated}
                catalog.setdefault(path,[]).append(new);code_nodes.append(new)
            except OSError:
                skips["unreadable"]=skips.get("unreadable",0)+1;continue
        else:
            classification=T.classify(path)
            if not classification and any(path.startswith(prefix) for prefix in T.PREFIXES):
                try:
                    raw=source_path(path).read_bytes()
                    if content_hash(raw)!=old["content_hash"]:
                        skips["telemetry_source_drift"]=skips.get("telemetry_source_drift",0)+1
                        continue
                    parsed=json.loads(raw)
                    schema_checked+=1
                    classification=T.classify(path,parsed)
                except (OSError,ValueError):
                    skips["telemetry_unreadable_or_invalid"]=skips.get("telemetry_unreadable_or_invalid",0)+1
                    continue
            if not classification:continue
            md.update(classification)
            updated.update(type="Event",family="event",
                           summary="Machine telemetry: "+(old["summary"] or "").removeprefix("Config keys: "),
                           metadata=json.dumps(md,ensure_ascii=False))
        before={key:old[key] for key in FIELDS}
        nodes.append({"id":old["id"],"path":path,"content_hash":old["content_hash"],
                      "before":before,"after":updated})
    ids={n["id"] for n in code_nodes}
    original = R.owned_edges([dict(row) for row in conn.execute("SELECT * FROM edges WHERE predicate IN ('calls','imports','called_by','imported_by') AND asserted_by IN ('STATIC_AST','STATIC_AST_QUALIFIED','INVERSE_DERIVATION')")], ids)
    generated=[];counts={"resolved":0,"unresolved":0,"cross_file":0,"self":0,"calls_cross_file":0}
    for n in code_nodes:
        for edge in R.resolved_edges(n,catalog):
            generated.append(edge)
            state=edge["metadata"]["resolution"];counts[state]+=1
            if state=="resolved":
                counts["self" if edge["to"]==edge["from"] else "cross_file"]+=1
                if edge["predicate"]=="calls" and edge["to"]!=edge["from"]:counts["calls_cross_file"]+=1
    result={"schema":"brain.cleanup4.plan.v1","nodes":nodes,"code_ids":sorted(ids),
            "edges_before":original,"edges_after":generated,
            "counts":{**counts,"code_files":len(ids),"telemetry_nodes":len(nodes)-len(ids),
                      "retired_edges":len(original),"schema_sources_checked":schema_checked,"skips":skips}}
    result["plan_sha256"]=digest(result)
    return result

def validate_plan(p):
    expected=p["plan_sha256"]
    actual=digest({k:v for k,v in p.items() if k!="plan_sha256"})
    if actual!=expected:raise ValueError("plan hash mismatch")
    for n in p["nodes"]:
        if not allowed(n["path"]):raise ValueError("source outside permitted roots")

def apply(conn,p,*,rollback=False):
    from .. import db as DB
    validate_plan(p)
    conn.row_factory=sqlite3.Row
    code_ids=set(p["code_ids"])
    conn.execute("BEGIN IMMEDIATE")
    try:
        expect="after" if rollback else "before"
        target="before" if rollback else "after"
        for n in p["nodes"]:
            row=conn.execute("SELECT path,type,family,summary,metadata,content_hash FROM nodes WHERE id=?",(n["id"],)).fetchone()
            if row is None or row["path"]!=n["path"] or any(row[k]!=n[expect][k] for k in FIELDS) or row["content_hash"]!=n["content_hash"]:
                raise ValueError("concurrent node change: "+n["id"])
            if not rollback and (n["id"] in code_ids or R._loads(n["after"]["metadata"]).get("classification_basis") == "collection-observation-schema"):
                if content_hash(source_path(n["path"]).read_bytes())!=n["content_hash"]:
                    raise ValueError("concurrent source change: "+n["path"])
        ids=set(p["code_ids"])
        current=R.owned_edges([dict(r) for r in conn.execute("SELECT * FROM edges WHERE predicate IN ('calls','imports','called_by','imported_by') AND asserted_by IN ('STATIC_AST','STATIC_AST_QUALIFIED','INVERSE_DERIVATION')")],ids)
        if not rollback:
            if sorted(current,key=lambda e:e["id"])!=sorted(p["edges_before"],key=lambda e:e["id"]):
                raise ValueError("concurrent edge change")
        else:
            def normalized(e):
                return (e["from_id"],e["to_id"],e["predicate"],e["asserted_by"],e["metadata"])
            expected={(e["from"],e["to"],e["predicate"],e["asserted_by"],json.dumps(e["metadata"])) for e in p["edges_after"]}
            if {normalized(e) for e in current}!=expected:
                raise ValueError("concurrent edge change since apply; reconcile before rollback")
        for e in current:
            conn.execute("DELETE FROM edges WHERE id=?",(e["id"],))
        fts_changes=[]
        for n in p["nodes"]:
            d=n[target]
            conn.execute("UPDATE nodes SET type=?,family=?,summary=?,metadata=? WHERE id=?",(*(d[k] for k in FIELDS),n["id"]))
            if d["summary"] != n[expect]["summary"]:
                fts_changes.append((n["id"],d["summary"]))
        if fts_changes:
            conn.execute("CREATE TEMP TABLE cleanup4_fts_changes(node_id TEXT PRIMARY KEY, summary TEXT)")
            conn.executemany("INSERT INTO cleanup4_fts_changes VALUES (?,?)",fts_changes)
            conn.execute("UPDATE fts_nodes SET summary=(SELECT summary FROM cleanup4_fts_changes c WHERE c.node_id=fts_nodes.node_id) WHERE node_id IN (SELECT node_id FROM cleanup4_fts_changes)")
            conn.execute("DROP TABLE cleanup4_fts_changes")
        if rollback:
            for e in p["edges_before"]:
                cols=list(e)
                conn.execute("INSERT INTO edges ("+",".join(cols)+") VALUES ("+",".join("?" for _ in cols)+")",tuple(e[c] for c in cols))
        else:
            for e in p["edges_after"]:DB.insert_edge(conn,e)
        conn.commit()
    except BaseException:
        conn.rollback();raise

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("operation",choices=["plan","apply","rollback"])
    ap.add_argument("--db",type=Path,default=C.BRAIN_DB)
    ap.add_argument("--plan",type=Path,required=True)
    args=ap.parse_args()
    if args.operation=="plan":
        conn=sqlite3.connect("file:"+str(args.db)+"?mode=ro",uri=True)
        conn.execute("BEGIN")
        p=plan(conn)
        text=json.dumps(p,ensure_ascii=False)
        # Same universal gate as the live surfaces; fail closed for this writer.
        from ..enforcement import run_gate
        verdict=run_gate("brain_validate_write",{"file_path":str(args.plan),"content":text,"tool_name":"Write"})
        if verdict.get("decision")!="allow" or verdict.get("errors"):raise RuntimeError(verdict)
        args.plan.parent.mkdir(parents=True,exist_ok=True)
        with args.plan.open("x") as f:
            f.write(text)
        args.plan.chmod(0o600)
        print(json.dumps({"plan_sha256":p["plan_sha256"],"counts":p["counts"]}))
    else:
        p=json.loads(args.plan.read_text())
        conn=sqlite3.connect(str(args.db),timeout=30)
        apply(conn,p,rollback=args.operation=="rollback")
        print(json.dumps({"operation":args.operation,"plan_sha256":p["plan_sha256"],"counts":p["counts"]}))
    conn.close()
if __name__=="__main__":main()

