"""Monthly brain compaction (Item 17).

Three phases — all idempotent + safe to interrupt mid-run.

  Phase A — near-duplicate merge (cosine ≥0.92 + same family + name overlap
            + Qwen 14B yes/no confirmation, thermal-gated). Uses the same
            reparenting machinery as `entity_dedup`.

  Phase B — supersession archival. For each node with `superseded_by` set:
            archive body excerpt to `99_ARCHIVE/superseded/<id>.md`, set
            `status='archived'`. Edges retained for graph queries.

  Phase C — orphan retirement candidates. Nodes with zero edges AND
            family='document' AND last_modified >180 days → write proposal
            entry to `staging/proposals/` for COVENANT review. Never auto-
            delete.

Run:
    python3 -m engine.maintenance.compactor [--dry-run] [--phases ABC]

Wired into `com.arcturion.altair-brain-compact` LaunchAgent (monthly 04:00 ET).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np

from .. import config as C
from .. import db as DB
from ..util import now_iso, short_hash

PHASE_A_COSINE_FLOOR = 0.92
ORPHAN_DAYS = 180
ARCHIVE_DIR = C.UNIVERSE_ROOT / "99_ARCHIVE" / "superseded"
STAGING_DIR = C.UNIVERSE_ROOT / "06_MEMORY_SYSTEM" / "0611_LLM_WIKI" / "staging" / "proposals"


# ── Phase A — near-duplicate merge ─────────────────────────────────────────

def _llm_confirm_same(title_a: str, summary_a: str,
                      title_b: str, summary_b: str) -> bool:
    """Ask Qwen 14B yes/no whether two entities are the same. Returns True only on explicit 'yes'."""
    try:
        sys.path.insert(0, str(C.UNIVERSE_ROOT / "11_INFRASTRUCTURE" / "1107_LOCAL_LLM"))
        from local_llm_client import chat_14b
    except Exception:
        return False
    prompt = (
        f"Are these the same entity? Answer with one word: yes or no.\n\n"
        f"Entity A:\n  title: {title_a}\n  summary: {summary_a[:200]}\n\n"
        f"Entity B:\n  title: {title_b}\n  summary: {summary_b[:200]}\n"
    )
    try:
        out = chat_14b(prompt, max_tokens=10, temperature=0.0, timeout=30,
                       fallback_to_7b=True).strip().lower()
    except Exception:
        return False
    return out.startswith("yes")


def _reparent(conn, loser: str, canonical: str) -> None:
    conn.execute("UPDATE OR IGNORE edges SET to_id=? WHERE to_id=?", (canonical, loser))
    conn.execute("UPDATE OR IGNORE edges SET from_id=? WHERE from_id=?", (canonical, loser))
    conn.execute("DELETE FROM edges WHERE from_id=? OR to_id=?", (loser, loser))
    conn.execute("DELETE FROM edges WHERE from_id = to_id")


def phase_a_merge(threshold: float = PHASE_A_COSINE_FLOOR, dry_run: bool = False) -> dict:
    conn = DB.connect()
    try:
        rows = conn.execute(
            """SELECT n.id, n.title, n.summary, n.family, n.created, e.vector, e.dim
               FROM nodes n
               JOIN embeddings e ON e.node_id = n.id
               WHERE n.status != 'superseded' AND n.status != 'archived'"""
        ).fetchall()
        if len(rows) < 2:
            return {"merged": 0, "candidates": 0}

        ids = [r["id"] for r in rows]
        titles = [r["title"] or "" for r in rows]
        summaries = [r["summary"] or "" for r in rows]
        families = [r["family"] or "" for r in rows]
        created = [r["created"] or "9999" for r in rows]
        dim = rows[0]["dim"]
        mat = np.zeros((len(rows), dim), dtype=np.float32)
        for i, r in enumerate(rows):
            mat[i] = np.frombuffer(r["vector"], dtype=np.float32)
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        mat = mat / norms

        sim = mat @ mat.T

        candidates: list[tuple[int, int, float]] = []
        n = len(rows)
        for i in range(n):
            for j in range(i + 1, n):
                if families[i] != families[j]:
                    continue
                if sim[i, j] < threshold:
                    continue
                a_tokens = set(titles[i].lower().split())
                b_tokens = set(titles[j].lower().split())
                if not (a_tokens & b_tokens):
                    continue
                candidates.append((i, j, float(sim[i, j])))
        candidates.sort(key=lambda t: -t[2])

        merged_ids: set[str] = set()
        confirmed_pairs: list[tuple[str, str, float]] = []
        for i, j, s in candidates:
            if ids[i] in merged_ids or ids[j] in merged_ids:
                continue
            if not _llm_confirm_same(titles[i], summaries[i], titles[j], summaries[j]):
                continue
            ci, li = (i, j) if created[i] <= created[j] else (j, i)
            canonical, loser = ids[ci], ids[li]
            confirmed_pairs.append((canonical, loser, s))
            merged_ids.add(loser)
            if not dry_run:
                _reparent(conn, loser, canonical)
                DB.insert_edge(conn, {
                    "from": loser, "to": canonical, "predicate": "superseded_by",
                    "family": "lifecycle", "weight": 1.0, "confidence": 1.0,
                    "asserted_by": "compactor_phase_a", "asserted_at": now_iso(),
                    "evidence_span": f"compactor near-duplicate merge cos={s:.3f}",
                    "metadata": {},
                })
                conn.execute("UPDATE nodes SET status='superseded' WHERE id=?", (loser,))
        if not dry_run:
            conn.commit()
        return {"candidates": len(candidates), "merged": len(confirmed_pairs),
                "pairs": confirmed_pairs[:20]}
    finally:
        conn.close()


# ── Phase B — supersession archival ────────────────────────────────────────

def phase_b_archive(dry_run: bool = False) -> dict:
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    conn = DB.connect()
    try:
        rows = conn.execute(
            """SELECT n.id, n.title, n.summary, n.body_excerpt, n.status, n.path, e.to_id AS canonical_id
               FROM nodes n
               LEFT JOIN edges e ON e.from_id = n.id AND e.predicate = 'superseded_by'
               WHERE (n.status = 'superseded' OR e.to_id IS NOT NULL)
                 AND n.status != 'archived'"""
        ).fetchall()
        archived = 0
        for r in rows:
            nid = r["id"]
            archive_path = ARCHIVE_DIR / f"{nid}.md"
            if archive_path.exists():
                continue
            body = (
                f"---\nid: {nid}\nstatus: archived\nsuperseded_by: {r['canonical_id'] or ''}\n"
                f"archived_at: {now_iso()}\noriginal_path: {r['path'] or ''}\n---\n\n"
                f"# {r['title'] or nid}\n\n{r['summary'] or ''}\n\n{r['body_excerpt'] or ''}\n"
            )
            if not dry_run:
                archive_path.write_text(body, encoding="utf-8")
                conn.execute("UPDATE nodes SET status='archived' WHERE id=?", (nid,))
            archived += 1
        if not dry_run:
            conn.commit()
        return {"archived": archived}
    finally:
        conn.close()


# ── Phase C — orphan retirement candidates ─────────────────────────────────

def phase_c_orphans(dry_run: bool = False) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=ORPHAN_DAYS)).isoformat(timespec="seconds")
    conn = DB.connect()
    try:
        rows = conn.execute(
            """SELECT id, title, path, last_modified FROM nodes n
               WHERE family = 'document' AND status NOT IN ('superseded','archived')
                 AND (last_modified IS NULL OR last_modified < ?)
                 AND id NOT IN (SELECT DISTINCT from_id FROM edges)
                 AND id NOT IN (SELECT DISTINCT to_id FROM edges)
               LIMIT 1000""",
            (cutoff,),
        ).fetchall()
        if not rows or dry_run:
            return {"candidates": len(rows)}
        STAGING_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        h = short_hash(f"compactor_orphans:{ts}")[:8]
        path = STAGING_DIR / f"{ts}_compactor_orphans_{h}.jsonl"
        with path.open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps({
                    "proposal_id": f"prop_{short_hash(r['id'])[:8]}",
                    "kind": "retirement",
                    "node_id": r["id"],
                    "title": r["title"],
                    "path": r["path"],
                    "last_modified": r["last_modified"],
                    "rationale": f"orphan >180 days (cutoff={cutoff})",
                    "asserted_by": "compactor_phase_c",
                    "asserted_at": now_iso(),
                }, ensure_ascii=False) + "\n")
        return {"candidates": len(rows), "staging_path": str(path.relative_to(C.UNIVERSE_ROOT))}
    finally:
        conn.close()


def run(phases: str = "ABC", dry_run: bool = False) -> dict:
    out = {"phases": phases, "dry_run": dry_run, "ts": now_iso()}
    if "A" in phases:
        out["phase_A"] = phase_a_merge(dry_run=dry_run)
    if "B" in phases:
        out["phase_B"] = phase_b_archive(dry_run=dry_run)
    if "C" in phases:
        out["phase_C"] = phase_c_orphans(dry_run=dry_run)
    return out


# Finite proposal-only lane. Existing scheduled phases are unchanged.
def _consolidation_inputs(conn, node_ids):
    from ..source_integrity import filter_retrieval, source_path
    import hashlib
    if not isinstance(node_ids, list) or not 1 <= len(node_ids) <= 20 or any(not isinstance(x,str) or not x or len(x)>200 for x in node_ids) or len(set(node_ids)) != len(node_ids):
        raise ValueError("consolidation requires 1..20 distinct explicit node IDs")
    inputs=[]; sections=[]; total=0
    for node_id in node_ids:
        row=conn.execute('SELECT * FROM nodes WHERE id=?',(node_id,)).fetchone()
        allowed=filter_retrieval(conn,[dict(row)] if row else [],historical=False)
        if not allowed:
            raise ValueError("consolidation input unavailable or excluded")
        node=allowed[0]; path=source_path(node.get('path'))
        if path is None or not path.is_file() or path.stat().st_size>65536:
            raise ValueError("consolidation requires a bounded canonical text source")
        with path.open('rb') as stream:
            raw=stream.read(65537)
        if len(raw)>65536:
            raise ValueError("consolidation source exceeds byte limit")
        total+=len(raw)
        if total>262144:
            raise ValueError("consolidation source batch exceeds byte limit")
        digest=hashlib.sha256(raw).hexdigest()
        if digest!=node.get('content_hash'):
            raise ValueError("consolidation source/index version drift")
        text=raw.decode('utf-8',errors='strict')
        inputs.append({'id':node_id,'content_hash':digest,'path':node['path']})
        sections.append({'source_id':node_id,'source_path':node['path'],'source_sha256':digest,'verbatim_text':text})
    return inputs,sections

def review_consolidation(proposal, conn=None):
    """Read-only eligibility review; never acceptance, publication or authority."""
    owned=conn is None
    conn=conn or DB.connect()
    try:
        if not isinstance(proposal,dict) or proposal.get('kind')!='consolidation' or proposal.get('schema')!='brain.consolidation-proposal.v1':
            raise ValueError("unsupported consolidation proposal")
        original=proposal.get('inputs')
        if not isinstance(original,list) or not all(isinstance(x,dict) and isinstance(x.get('id'),str) for x in original):
            raise ValueError("invalid consolidation inputs")
        inputs,sections=_consolidation_inputs(conn,[x['id'] for x in original])
        if original!=inputs or proposal.get('sections')!=sections:
            raise ValueError("stale or altered consolidation proposal")
        expected=[{'relation':'derived_from','kind':'brain','id':x['id'],'content_hash':x['content_hash']} for x in inputs]
        if proposal.get('dependencies')!=expected:
            raise ValueError("invalid consolidation dependency bindings")
        if proposal.get('authority')!='none' or proposal.get('publication_eligible') is not False:
            raise ValueError("proposal cannot confer authority or publication")
        return {'status':'current_for_review','inputs_current':True,'publication_eligible':False,
                'requires_reviews':['COVENANT','ALTAIR'],'canonical_writes':0}
    finally:
        if owned:conn.close()

def propose_consolidation(node_ids):
    """Stage only complete source text and exact attribution; no model or inbox."""
    from .. import mcp_server as MCP
    import hashlib
    conn=DB.connect()
    try:
        inputs,sections=_consolidation_inputs(conn,node_ids)
        material=json.dumps({'inputs':inputs,'sections':sections},sort_keys=True,ensure_ascii=False)
        proposal={'schema':'brain.consolidation-proposal.v1',
            'proposal_id':'prop_consolidation_'+hashlib.sha256(material.encode()).hexdigest()[:20],
            'kind':'consolidation','inputs':inputs,'sections':sections,
            'dependencies':[{'relation':'derived_from','kind':'brain','id':x['id'],'content_hash':x['content_hash']} for x in inputs],
            'authority':'none','publication_eligible':False,
            'asserted_by':'bounded_consolidation','asserted_at':now_iso(),
            'rationale':'Extractive evidence bundle for human/domain review; no new generalization or permission.'}
        # Existing source-secret guard and writer are reused without queue notifications.
        if MCP.V.scan_secrets(material):
            raise ValueError("consolidation source secret guard refused")
        review_consolidation(proposal,conn)
        path=MCP._write_proposal([proposal],source='bounded_consolidation')
        path.chmod(0o600)
        return {'proposal_id':proposal['proposal_id'],'staged_path':str(path),'inputs':len(inputs),
                'publication_eligible':False,'canonical_writes':0}
    finally:
        conn.close()

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phases", default="ABC")
    ap.add_argument("--dry-run", action="store_true")
    lane=ap.add_mutually_exclusive_group()
    lane.add_argument('--propose-consolidation',nargs='+',metavar='NODE_ID')
    lane.add_argument('--review-consolidation',type=Path,metavar='PROPOSAL_JSONL')
    args = ap.parse_args()
    if args.propose_consolidation:
        if args.dry_run:
            ap.error('proposal-only lane stages review evidence; omit --dry-run')
        print(json.dumps(propose_consolidation(args.propose_consolidation),indent=2))
        return 0
    if args.review_consolidation:
        if args.review_consolidation.stat().st_size>1048576:
            raise ValueError('proposal exceeds review size limit')
        lines=args.review_consolidation.read_text().splitlines()
        if len(lines)!=1:raise ValueError('review requires exactly one proposal')
        print(json.dumps(review_consolidation(json.loads(lines[0])),indent=2))
        return 0
    print(json.dumps(run(phases=args.phases, dry_run=args.dry_run), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
