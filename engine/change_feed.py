"""Owner change feed, staged behind an explicit disabled-by-default activation flag.

Importing performs no migration. Normal init and HTTP hooks require
ARCTURION_BRAIN_CHANGE_FEED=1; joint owner/admission review precedes activation.
Journal contains no body. Loopback is the existing shared-account trust baseline.
"""
from __future__ import annotations
import base64
import json
import uuid
from . import capacity_admission as A
from .source_integrity import filter_retrieval

COVERAGE=("Current allowed versions in admitted assignments only. Deleted and "
          "superseded versions without a current privacy-approved node expose no payload. "
          "Loopback admission does not independently authenticate a requester principal.")
_FIELDS=("content_hash","path","metadata","title","type","family","owner","status",
         "last_modified","last_reviewed","valid_until")
def install(conn):
    """Explicit owner migration only; caller supplies its normal writable index connection."""
    conn.execute("SAVEPOINT install_change_feed")
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS change_feed_config(epoch TEXT NOT NULL)")
        existing=conn.execute("SELECT epoch FROM change_feed_config").fetchall()
        if len(existing)>1:raise ValueError("Invalid change-feed generation state")
        conn.execute("""CREATE TABLE IF NOT EXISTS node_change_journal(
            seq INTEGER PRIMARY KEY AUTOINCREMENT,node_id TEXT NOT NULL,
            operation TEXT NOT NULL,content_hash TEXT,observed_at TEXT NOT NULL)""")
        conn.execute("CREATE INDEX IF NOT EXISTS node_change_journal_node ON node_change_journal(node_id,seq)")
        observed="strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        conn.execute("""CREATE TRIGGER IF NOT EXISTS node_change_insert AFTER INSERT ON nodes BEGIN
            INSERT INTO node_change_journal(node_id,operation,content_hash,observed_at)
            VALUES(NEW.id,'insert',NEW.content_hash,"""+observed+"); END")
        when=" OR ".join("OLD."+f+" IS NOT NEW."+f for f in _FIELDS)
        conn.execute("CREATE TRIGGER IF NOT EXISTS node_change_update AFTER UPDATE OF "+",".join(_FIELDS)+
            " ON nodes WHEN "+when+" BEGIN INSERT INTO node_change_journal(node_id,operation,content_hash,observed_at) "+
            "VALUES(NEW.id,'update',NEW.content_hash,"+observed+"); END")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS node_change_delete AFTER DELETE ON nodes BEGIN
            INSERT INTO node_change_journal(node_id,operation,content_hash,observed_at)
            VALUES(OLD.id,'delete',OLD.content_hash,"""+observed+"); END")
        if not existing:
            conn.execute("INSERT INTO change_feed_config(epoch) VALUES(?)",(uuid.uuid4().hex,))
            conn.execute("INSERT INTO node_change_journal(node_id,operation,content_hash,observed_at) "+
                         "SELECT id,'snapshot',content_hash,"+observed+" FROM nodes ORDER BY id")
        conn.execute("RELEASE install_change_feed")
    except Exception:
        conn.execute("ROLLBACK TO install_change_feed");conn.execute("RELEASE install_change_feed")
        raise
def _encode(epoch,scope,seq):
    raw=json.dumps({"v":1,"epoch":epoch,"scope":scope,"seq":seq},sort_keys=True,separators=(",",":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
def _decode(value,epoch,scope):
    if value is None:return 0
    if not isinstance(value,str) or not 1<=len(value)<=2000:raise ValueError("Invalid change cursor")
    try:
        record=json.loads(base64.b64decode(value+"="*((-len(value))%4),altchars=b"-_",validate=True))
    except (ValueError,UnicodeError):raise ValueError("Invalid change cursor") from None
    if (not isinstance(record,dict) or set(record)!={"v","epoch","scope","seq"} or record["v"]!=1
            or record["epoch"]!=epoch or record["scope"]!=scope
            or type(record["seq"]) is not int or record["seq"]<0):
        raise ValueError("Cursor generation or admitted scope changed; explicit rebaseline required")
    return record["seq"]
def changes(*,capacity="business",project_id=None,cursor=None,limit=100,bootstrap=False):
    if type(limit) is not int or not 1<=limit<=200:raise ValueError("Change limit must be1..200")
    if type(bootstrap) is not bool or (bootstrap and cursor is not None):
        raise ValueError("Bootstrap is a fixed boolean mode exclusive with cursor")
    if bootstrap:limit=min(limit,25)
    conn=A.scoped_connection(capacity,project_id,include_telemetry=False)
    try:
        tables={r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type='table' "
                                         "AND name IN ('change_feed_config','node_change_journal')")}
        if len(tables)!=2:raise ValueError("Owner change journal not installed; activation review required")
        configs=conn.execute("SELECT epoch FROM change_feed_config").fetchall()
        if len(configs)!=1 or not configs[0][0]:raise ValueError("Invalid change-feed generation")
        epoch=configs[0][0];scope=A.scope_key(conn);after=_decode(cursor,epoch,scope)
        head=conn.execute("SELECT COALESCE(MAX(seq),0) FROM node_change_journal").fetchone()[0]
        if after>head:raise ValueError("Change cursor is beyond journal head; explicit rebaseline required")
        if bootstrap:
            events=conn.execute("SELECT seq,node_id,operation,content_hash,observed_at FROM node_change_journal "
                                "WHERE seq<=? ORDER BY seq DESC LIMIT 200",(head,)).fetchall()
        else:
            events=conn.execute("SELECT seq,node_id,operation,content_hash,observed_at FROM node_change_journal "
                                "WHERE seq>? ORDER BY seq LIMIT ?",(after,max(200,limit*5))).fetchall()
        records=[];next_seq=head if bootstrap else after
        for event in events:
            if not bootstrap:next_seq=event["seq"]
            if event["operation"]=="delete":continue
            # Metadata is current, not an invented historical snapshot of an
            # overwritten node. Earlier same-hash status/title changes are omitted.
            newest=conn.execute("SELECT MAX(seq) FROM node_change_journal WHERE node_id=?",
                                (event["node_id"],)).fetchone()[0]
            if event["seq"]!=newest:continue
            # TEMP nodes is the same pinned, admission-gated view used by retrieval.
            row=conn.execute("SELECT * FROM nodes WHERE id=?",(event["node_id"],)).fetchone()
            if row is None or row["content_hash"]!=event["content_hash"]:continue
            full=dict(row)
            if not filter_retrieval(conn,[full]):continue
            record={key:full.get(key) for key in ("id","title","path","owner","status","type","family",
                                                  "last_modified","content_hash")}
            record.update(sequence=event["seq"],operation=event["operation"],observed_at=event["observed_at"],
                          verification_cursor=_encode(epoch,scope,event["seq"]-1),
                          historical=event["operation"]=="snapshot")
            records.append(record)
            if len(records)>=limit:break
        records.sort(key=lambda row:row["sequence"])
        coverage=COVERAGE + (" Initial bounded current metadata sample; journal sequence is not provider recency. Prior history remains queryable but is not automatically imported." if bootstrap else "")
        return {"schema":"brain.changes.v1","capacity":capacity,"project_id":project_id,"bootstrap":bootstrap,
                "records":records,"next_cursor":_encode(epoch,scope,next_seq),"has_more":False if bootstrap else next_seq<head,
                "coverage":coverage,"source_scope":A.coverage_warning(capacity,project_id)}
    finally:conn.close()
def http_response(qs):
    """GET /changes parser; selectors are bound by the fixed workforce resource.

    Direct loopback callers remain trusted local processes, as on existing reads.
    An opaque cursor binds scope/generation; it is not an access credential.
    """
    if set(qs)-{"capacity","project_id","cursor","limit","bootstrap"} or any(
            not isinstance(v,list) or len(v)!=1 for v in qs.values()):
        raise ValueError("Unknown or repeated change-feed parameters")
    def one(key,default=None):return qs.get(key,[default])[0]
    if one("capacity") not in ("business", "personal"):
        raise ValueError("Explicit business or personal capacity required")
    if any(not isinstance(v[0],str) or not v[0] for v in qs.values()):
        raise ValueError("Blank change-feed parameters are not allowed")
    raw=one("limit","100")
    if not isinstance(raw,str) or not raw.isdigit():raise ValueError("Invalid change limit")
    boot=one("bootstrap","false")
    if boot not in ("true","false"):raise ValueError("Bootstrap must be true or false")
    return changes(capacity=one("capacity"),project_id=one("project_id"),
                   cursor=one("cursor"),limit=int(raw),bootstrap=boot=="true")
