"""SQLite schema + helpers for brain.sqlite (L4 query index over JSONL canon)."""

from __future__ import annotations
import sqlite3
import json
from pathlib import Path
from typing import Iterable
from . import config as C


EDGE_ADMISSION_QUARANTINE_SCHEMA = """
CREATE TABLE IF NOT EXISTS edge_admission_quarantine (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_id TEXT NOT NULL,
  to_id TEXT NOT NULL,
  predicate TEXT NOT NULL,
  asserted_by TEXT NOT NULL,
  reason TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  times INTEGER NOT NULL DEFAULT 1,
  UNIQUE(from_id, to_id, predicate, asserted_by, reason)
);
"""

SOURCE_WATERMARK_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_ingest_watermarks (
  source_key TEXT PRIMARY KEY,
  source_path TEXT,
  source_kind TEXT NOT NULL,
  content_hash TEXT,
  source_mtime_ns INTEGER,
  event_watermark TEXT,
  node_id TEXT,
  status TEXT NOT NULL,
  last_observed_at TEXT NOT NULL,
  last_success_at TEXT,
  last_error TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_watermarks_path
  ON source_ingest_watermarks(source_path);
CREATE INDEX IF NOT EXISTS idx_source_watermarks_success
  ON source_ingest_watermarks(last_success_at);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  family TEXT NOT NULL,
  title TEXT,
  path TEXT,
  owner TEXT,
  status TEXT,
  governance_tier INTEGER DEFAULT 0,
  content_hash TEXT,
  summary TEXT,
  body_excerpt TEXT,
  centrality REAL DEFAULT 0.0,
  created TEXT,
  last_modified TEXT,
  last_reviewed TEXT,
  metadata TEXT
);
-- valid_from / valid_until (Leg 4, retrieval-honesty 2026-08-06) added via ALTER TABLE in
-- init_db, matching the prong/asserted_in_session additive-column pattern below — a fresh
-- CREATE TABLE (empty DB / test fixtures) still gets them from the ALTER, so this literal
-- schema block is intentionally left without them to keep ONE migration path, not two.

CREATE TABLE IF NOT EXISTS edges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  from_id TEXT NOT NULL,
  to_id TEXT NOT NULL,
  predicate TEXT NOT NULL,
  family TEXT NOT NULL,
  weight REAL DEFAULT 1.0,
  confidence REAL DEFAULT 1.0,
  asserted_by TEXT,
  asserted_at TEXT,
  evidence_span TEXT,
  metadata TEXT,
  UNIQUE(from_id, to_id, predicate, asserted_by)
);

CREATE TABLE IF NOT EXISTS aliases (
  alias TEXT PRIMARY KEY,
  canonical_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS embeddings (
  node_id TEXT PRIMARY KEY,
  dim INTEGER NOT NULL,
  vector BLOB NOT NULL,
  generated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_id);
CREATE INDEX IF NOT EXISTS idx_edges_to ON edges(to_id);
CREATE INDEX IF NOT EXISTS idx_edges_pred ON edges(predicate);
CREATE INDEX IF NOT EXISTS idx_edges_family ON edges(family);
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_family ON nodes(family);
CREATE INDEX IF NOT EXISTS idx_nodes_path ON nodes(path);
CREATE INDEX IF NOT EXISTS idx_nodes_owner ON nodes(owner);
CREATE INDEX IF NOT EXISTS idx_nodes_last_modified ON nodes(last_modified);
CREATE INDEX IF NOT EXISTS idx_nodes_created ON nodes(created);
CREATE INDEX IF NOT EXISTS idx_edges_asserted_at ON edges(asserted_at);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_nodes USING fts5(
  node_id UNINDEXED,
  title,
  summary,
  body,
  tokenize='porter unicode61'
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    p = db_path or C.BRAIN_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Live incremental ingest (engine.ingest) can write concurrently with the
    # nightly full reindex and with each other. WAL allows one writer at a time;
    # without a busy_timeout a second writer fails immediately with "database is
    # locked". Wait up to 30s so concurrent writers queue instead of erroring.
    conn.execute("PRAGMA busy_timeout=30000")
    # Read-path tuning (perf, 2026-06-20). mmap maps the ~3 GB index so the hot
    # working set stays resident across the long-lived brain-http / MCP processes
    # (kills the cold-page /query tail); cache_size is per-connection and modest;
    # temp_store=MEMORY keeps the ORDER BY/dedup sorts off disk. Read-only effect —
    # query results are unchanged. PRAGMA failures are non-fatal (best-effort).
    try:
        conn.execute(f"PRAGMA cache_size=-{C.SQLITE_CACHE_KIB}")
        conn.execute(f"PRAGMA mmap_size={C.SQLITE_MMAP_BYTES}")
        conn.execute("PRAGMA temp_store=MEMORY")
    except sqlite3.OperationalError:
        pass
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.executescript(EDGE_ADMISSION_QUARANTINE_SCHEMA)
    conn.executescript(SOURCE_WATERMARK_SCHEMA)
    from .source_integrity import SCHEMA as RETRIEVAL_QUARANTINE_SCHEMA
    conn.executescript(RETRIEVAL_QUARANTINE_SCHEMA)
    try:
        conn.executescript(FTS_SCHEMA)
    except sqlite3.OperationalError:
        # FTS5 unavailable on this build; brain.query falls back to LIKE.
        pass
    from .source_integrity import install_privacy_guards
    install_privacy_guards(conn)
    # Four-Prong Canon (2026-06-09): tag every node with its prong
    # (universe|atlas|constellation|runtime). Additive + idempotent — existing
    # rows stay NULL until the next --full reindex backfills them.
    try:
        conn.execute("ALTER TABLE nodes ADD COLUMN prong TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_prong ON nodes(prong)")
    # Brain v2 Phase 4 (2026-06-16): tag each edge with the session it was asserted in
    # (the cross-agent session/temporal layer). Additive + idempotent — existing rows
    # stay NULL; only the sessions harvester populates it. Mirrors the prong pattern.
    try:
        conn.execute("ALTER TABLE edges ADD COLUMN asserted_in_session TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute("CREATE INDEX IF NOT EXISTS idx_edges_session ON edges(asserted_in_session)")
    # Temporal validity (Leg 4, retrieval-honesty 2026-08-06): `valid_from` is stamped once
    # at first ingest (upsert_node, below) from created/last_modified; `valid_until` is set
    # ONLY by engine.maintenance.validity_pass from an explicit supersession/contradiction
    # record naming the node — never inferred, never overwritten by a reindex. Additive +
    # idempotent, mirrors the prong/asserted_in_session pattern: existing rows stay NULL
    # (behave exactly as before) until the pass or a future insert stamps them.
    try:
        conn.execute("ALTER TABLE nodes ADD COLUMN valid_from TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    try:
        conn.execute("ALTER TABLE nodes ADD COLUMN valid_until TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_valid_until ON nodes(valid_until)")
    # Staged owner integration: ordinary startup is unchanged without exact opt-in.
    if C.CHANGE_FEED_ENABLED:
        from .change_feed import install
        install(conn)
    conn.commit()


def upsert_node(conn: sqlite3.Connection, node: dict) -> None:
    from .source_integrity import record_privacy
    record_privacy(conn, node)
    md = json.dumps(node.get("metadata", {}), ensure_ascii=False)
    # Brain v2 (2026-06-16): never store a NULL prong. If a caller (e.g. a harvester
    # that builds node dicts directly) omits it, derive from the stored path so the
    # Commandment-017 prong invariant stays self-healing on every write.
    from . import config as _C
    prong = node.get("prong") or _C.prong_for_relpath(node.get("path") or "")
    # valid_from (Leg 4): derived from created, falling back to last_modified, at the
    # node's FIRST admission only. Deliberately absent from the ON CONFLICT SET clause
    # below — a re-ingest (incremental watch or nightly reindex) must never move a node's
    # origin point, and valid_until is never touched here at all: it is exclusively
    # engine.maintenance.validity_pass's to set, from an explicit record, never from a
    # content upsert.
    valid_from = node.get("valid_from") or node.get("created") or node.get("last_modified")
    conn.execute(
        """INSERT INTO nodes (id, type, family, title, path, owner, status,
            governance_tier, content_hash, summary, body_excerpt, centrality,
            created, last_modified, last_reviewed, metadata, prong, valid_from)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
             type=excluded.type, family=excluded.family, title=excluded.title,
             path=excluded.path, owner=excluded.owner, status=excluded.status,
             governance_tier=excluded.governance_tier,
             content_hash=excluded.content_hash, summary=excluded.summary,
             body_excerpt=excluded.body_excerpt, last_modified=excluded.last_modified,
             last_reviewed=excluded.last_reviewed, metadata=excluded.metadata,
             prong=excluded.prong""",
        (
            node["id"], node["type"], node["family"], node.get("title"),
            node.get("path"), node.get("owner"), node.get("status", "active"),
            node.get("governance_tier", 0), node.get("content_hash"),
            node.get("summary"), node.get("body_excerpt"), node.get("centrality", 0.0),
            node.get("created"), node.get("last_modified"), node.get("last_reviewed"),
            md, prong, valid_from,
        ),
    )
    if conn.execute("SELECT 1 FROM node_retrieval_quarantine WHERE (node_id=? OR (content_hash!='' AND reason!='restricted-rollback-source-2026-10-02')) AND content_hash=? AND scope IN ('all','restricted-source')",
                    (node['id'], node.get('content_hash') or '')).fetchone():
        return
    # Applies to the configured graph and source inventory.
    try:
        conn.execute("DELETE FROM fts_nodes WHERE node_id = ?", (node["id"],))
        conn.execute(
            "INSERT INTO fts_nodes (node_id, title, summary, body) VALUES (?,?,?,?)",
            (node["id"], node.get("title") or "", node.get("summary") or "",
             (node.get("body_excerpt") or "")[:C.FTS_BODY_CHARS]),
        )
    except sqlite3.OperationalError:
        pass


def quarantine_edge(conn: sqlite3.Connection, edge: dict, reason: str) -> None:
    """Durably retain a rejected edge so admission never becomes silent data loss."""
    predicate = str(edge.get("predicate") or "")
    asserted_by = str(edge.get("asserted_by") or "<unknown>")
    try:
        conn.execute(
            """INSERT INTO edge_admission_quarantine
               (from_id, to_id, predicate, asserted_by, reason, payload_json)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(from_id, to_id, predicate, asserted_by, reason)
               DO UPDATE SET payload_json=excluded.payload_json,
                             last_seen=CURRENT_TIMESTAMP,
                             times=edge_admission_quarantine.times+1""",
            (
                str(edge.get("from") or edge.get("from_id") or ""),
                str(edge.get("to") or edge.get("to_id") or ""),
                predicate,
                asserted_by,
                reason,
                json.dumps(edge, ensure_ascii=False, sort_keys=True, default=str),
            ),
        )
    except sqlite3.Error as exc:
        raise sqlite3.IntegrityError(
            f"edge rejected and quarantine write failed for predicate {predicate!r}: {exc}"
        ) from exc


def insert_edge(conn: sqlite3.Connection, edge: dict) -> bool:
    """Insert one active-canon edge; quarantine unknown/retired predicates.

    Returns True when inserted and False for either an existing duplicate or a
    durably quarantined predicate.  Callers that need the distinction can inspect
    ``edge_admission_quarantine``; no rejected payload is silently discarded.
    """
    from . import validator as V

    predicate = str(edge.get("predicate") or "")
    if predicate not in V.ACTIVE_PREDICATE_CANON:
        reason = (
            "retired_predicate" if predicate in V.RETIRED_PREDICATES
            else "unknown_predicate"
        )
        quarantine_edge(conn, edge, reason)
        return False

    metadata = edge.get("metadata") if isinstance(edge.get("metadata"), dict) else {}
    invalid = metadata.get("invalid_endpoint_reason")
    if invalid:
        quarantine_edge(conn, edge, str(invalid))
        return False

    # Producers may declare a stable endpoint node beside the edge.  Materialize that
    # node before admitting the edge so legitimate infrastructure relationships are
    # traversable in the same transaction.  The declaration itself remains in edge
    # metadata as provenance.
    endpoint = metadata.get("endpoint_node")
    if isinstance(endpoint, dict):
        endpoint_id = str(endpoint.get("id") or "")
        if not endpoint_id or endpoint_id != str(edge.get("to") or ""):
            quarantine_edge(conn, edge, "invalid_endpoint_declaration")
            return False
        if conn.execute("SELECT 1 FROM nodes WHERE id=?", (endpoint_id,)).fetchone() is None:
            required = ("type", "family", "title")
            if any(not endpoint.get(k) for k in required):
                quarantine_edge(conn, edge, "incomplete_endpoint_declaration")
                return False
            upsert_node(conn, {
                "id": endpoint_id,
                "type": endpoint["type"],
                "family": endpoint["family"],
                "title": endpoint["title"],
                "path": endpoint.get("path"),
                "owner": endpoint.get("owner"),
                # An edge producer proves only that an endpoint is declared/configured.
                # Runtime harvesters may later promote it after an observation, but an
                # inferred filename must never become an apparently live service.
                "status": endpoint.get("status", "configured"),
                "governance_tier": endpoint.get("governance_tier", 0),
                "content_hash": endpoint.get("content_hash"),
                "summary": endpoint.get("summary"),
                "body_excerpt": None,
                "centrality": 0.0,
                "created": endpoint.get("created"),
                "last_modified": endpoint.get("last_modified") or edge.get("asserted_at"),
                "last_reviewed": None,
                "metadata": {
                    **(endpoint.get("metadata")
                       if isinstance(endpoint.get("metadata"), dict) else {}),
                    "materialized_from_edge": True,
                    "asserted_by": edge.get("asserted_by"),
                    "evidence_span": edge.get("evidence_span"),
                },
            })

    cur = conn.execute(
        """INSERT OR IGNORE INTO edges (from_id, to_id, predicate, family,
            weight, confidence, asserted_by, asserted_at, evidence_span, metadata,
            asserted_in_session)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            edge["from"], edge["to"], predicate, edge.get("family", "structure"),
            edge.get("weight", 1.0), edge.get("confidence", 1.0),
            edge.get("asserted_by", "STATIC"), edge.get("asserted_at"),
            edge.get("evidence_span"), json.dumps(edge.get("metadata", {})),
            edge.get("asserted_in_session"),
        ),
    )
    return cur.rowcount > 0


def bulk_insert_edges(conn: sqlite3.Connection, edges: Iterable[dict]) -> int:
    count = 0
    for e in edges:
        if insert_edge(conn, e):
            count += 1
    return count


def record_source_watermark(
    conn: sqlite3.Connection,
    *,
    source_key: str,
    source_kind: str,
    observed_at: str,
    success: bool,
    source_path: str | None = None,
    content_hash: str | None = None,
    source_mtime_ns: int | None = None,
    event_watermark: str | None = None,
    node_id: str | None = None,
    error: str | None = None,
) -> None:
    """Record source-level observation while preserving the last proven success.

    A failed attempt advances ``last_observed_at`` and records the error, but cannot
    refresh ``last_success_at`` or replace the content/event watermark.  This prevents
    a busy failing writer from making stale sources look current.
    """
    conn.execute(
        """INSERT INTO source_ingest_watermarks
             (source_key, source_path, source_kind, content_hash, source_mtime_ns,
              event_watermark, node_id, status, last_observed_at, last_success_at,
              last_error)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_key) DO UPDATE SET
             source_path=excluded.source_path,
             source_kind=excluded.source_kind,
             content_hash=CASE WHEN excluded.status='success'
                               THEN excluded.content_hash ELSE content_hash END,
             source_mtime_ns=CASE WHEN excluded.status='success'
                                  THEN excluded.source_mtime_ns ELSE source_mtime_ns END,
             event_watermark=CASE WHEN excluded.status='success'
                                  THEN excluded.event_watermark ELSE event_watermark END,
             node_id=CASE WHEN excluded.status='success'
                          THEN excluded.node_id ELSE node_id END,
             status=excluded.status,
             last_observed_at=excluded.last_observed_at,
             last_success_at=CASE WHEN excluded.status='success'
                                  THEN excluded.last_success_at ELSE last_success_at END,
             last_error=excluded.last_error""",
        (
            source_key, source_path, source_kind, content_hash, source_mtime_ns,
            event_watermark, node_id, "success" if success else "error", observed_at,
            observed_at if success else None, None if success else (error or "unknown error"),
        ),
    )


def replace_aliases(conn: sqlite3.Connection, alias_to_canonical: dict[str, str]) -> None:
    # Atomic delete+insert — a crash between the two statements permanently
    # wiped every alias (review 2026-07-20).
    with conn:
        conn.execute("DELETE FROM aliases")
        conn.executemany(
            "INSERT OR REPLACE INTO aliases (alias, canonical_id) VALUES (?, ?)",
            sorted(alias_to_canonical.items()),
        )


def upsert_embedding(conn: sqlite3.Connection, node_id: str, vector: bytes, dim: int, ts: str) -> None:
    conn.execute(
        """INSERT INTO embeddings (node_id, dim, vector, generated_at)
           VALUES (?,?,?,?)
           ON CONFLICT(node_id) DO UPDATE SET vector=excluded.vector,
             dim=excluded.dim, generated_at=excluded.generated_at""",
        (node_id, dim, vector, ts),
    )


def stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM nodes) AS nodes,"
        "       (SELECT COUNT(*) FROM edges) AS edges,"
        "       (SELECT COUNT(*) FROM aliases) AS aliases,"
        "       (SELECT COUNT(*) FROM embeddings) AS embeddings"
    ).fetchone()
    return dict(row) if row else {}


# Predicates the machine derives in bulk rather than asserts from a source document.
# They are regenerated continuously by the embedder, so their edges are almost always
# fresh and walkable — which means counting them toward graph health lets a busy
# embedder mask arbitrary rot in the asserted graph. Health is reported both ways:
# `walkable_pct` over everything, and `structural_walkable_pct` with these removed.
# The structural number is the one that means something.
DERIVED_PREDICATES = frozenset({"semantically_similar_to", "co_occurs_with"})


def edge_health(conn: sqlite3.Connection) -> dict:
    'Walkable-vs-dangling edge census — the honest size of the graph.'
    rows = conn.execute(
        """SELECT e.predicate AS predicate,
                  COUNT(*)    AS total,
                  SUM(CASE WHEN EXISTS(SELECT 1 FROM nodes n WHERE n.id = e.from_id)
                            AND EXISTS(SELECT 1 FROM nodes n WHERE n.id = e.to_id)
                           THEN 1 ELSE 0 END) AS walkable
             FROM edges e
            GROUP BY e.predicate"""
    ).fetchall()

    def _pct(part: int, whole: int) -> float | None:
        return round(100.0 * part / whole, 2) if whole else None

    total = walkable = struct_total = struct_walkable = 0
    dangling_by_predicate: dict[str, int] = {}
    fully_dangling: list[str] = []

    for r in rows:
        pred, n, ok = r["predicate"], int(r["total"]), int(r["walkable"] or 0)
        total += n
        walkable += ok
        if pred not in DERIVED_PREDICATES:
            struct_total += n
            struct_walkable += ok
        if ok < n:
            dangling_by_predicate[pred] = n - ok
        # A predicate with no walkable edge at all is a dead relationship class — the
        # whole vocabulary entry is unreachable, not merely damaged. Name it.
        if n and ok == 0:
            fully_dangling.append(pred)

    return {
        "edges_total": total,
        "edges_walkable": walkable,
        "edges_dangling": total - walkable,
        "walkable_pct": _pct(walkable, total),
        "structural_total": struct_total,
        "structural_walkable": struct_walkable,
        "structural_walkable_pct": _pct(struct_walkable, struct_total),
        "derived_predicates": sorted(DERIVED_PREDICATES),
        "dangling_by_predicate": dict(
            sorted(dangling_by_predicate.items(), key=lambda kv: -kv[1])
        ),
        "fully_dangling_predicates": sorted(fully_dangling),
    }


def family_counts(conn: sqlite3.Connection, table: str = "nodes") -> dict:
    # `table` is interpolated into SQL — allowlist it (review 2026-07-20).
    if table not in ("nodes", "edges"):
        raise ValueError(f"family_counts: invalid table {table!r}")
    rows = conn.execute(f"SELECT family, COUNT(*) AS c FROM {table} GROUP BY family").fetchall()
    return {r["family"]: r["c"] for r in rows}


def predicate_counts(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT predicate, COUNT(*) AS c FROM edges GROUP BY predicate ORDER BY c DESC").fetchall()
    return {r["predicate"]: r["c"] for r in rows}


def orphan_ids(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT id FROM nodes
           WHERE id NOT IN (SELECT DISTINCT from_id FROM edges)
             AND id NOT IN (SELECT DISTINCT to_id FROM edges)"""
    ).fetchall()
    return [r["id"] for r in rows]
