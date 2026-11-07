"""Read-only, connection-local capacity admission BEFORE any node payload retrieval.

The policy inputs must come from verified assignment/registry metadata, never from
an arbitrary caller-supplied path or owner. Existing privacy/quarantine filters
remain independently mandatory. This module reads no source document bodies.
"""
from dataclasses import dataclass
import json
from pathlib import PurePosixPath


def normalized_path(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("admission paths must be normalized absolute paths")
    p = PurePosixPath(value)
    if not p.is_absolute() or ".." in p.parts or str(p) != value:
        raise ValueError("admission paths must be normalized absolute paths")
    return str(p)


@dataclass(frozen=True)
class AdmissionPolicy:
    capacity: str = "business"
    read_roots: tuple = ()
    shared_sources: tuple = ()
    read_sources: tuple = ()

    def __post_init__(self):
        if self.capacity not in ("business", "personal"):
            raise ValueError("capacity must be business or personal")
        for field in ("read_roots", "shared_sources", "read_sources"):
            values = getattr(self, field)
            if not isinstance(values, (list, tuple)):
                raise ValueError("admission lists must be arrays")
            object.__setattr__(self, field, tuple(normalized_path(v) for v in values))
        if not (self.read_roots or self.read_sources or self.shared_sources):
            raise ValueError("admission requires verified assignment roots or exact sources")

    def allows(self, path, metadata):
        try:
            path = normalized_path(path)
            meta = json.loads(metadata) if isinstance(metadata, str) else metadata
            if meta is None:
                meta = {}
            if not isinstance(meta, dict):
                return False
            aliases = {"business":"business", "arcturion":"business",
                       "personal":"personal", "polarion":"personal", "shared":"shared"}
            labels = set()
            for key in ("capacity", "world"):
                if key in meta:
                    value = meta[key]
                    if not isinstance(value, str) or value.lower() not in aliases:
                        return False
                    labels.add(aliases[value.lower()])
            if len(labels) > 1:
                return False
            label = next(iter(labels), None)
            # Exact shared admission cannot override an explicitly private label.
            if path in self.shared_sources:
                return label in (None, "shared")
            if label is not None and label != self.capacity:
                return False
            # Unknown metadata is admitted ONLY by the already-classified registry
            # assignment roots/exact sources. Labels alone never grant access.
            return path in self.read_sources or any(
                path == root or path.startswith(root.rstrip("/") + "/")
                for root in self.read_roots)
        except (TypeError, ValueError):
            return False


def _candidate_sql(policy):
    """Conservative storage-path superset. Full policy still decides."""
    exact=set()
    roots=set()
    def variants(value):
        path=Path(value)
        values={value}
        for base in (C.UNIVERSE_ROOT,C.DESKTOP_ROOT):
            if path.is_relative_to(base):
                values.add(path.relative_to(base).as_posix())
        if path.is_relative_to(C.WORK_ROOT):
            relative=path.relative_to(C.WORK_ROOT).as_posix()
            if relative.startswith(".claude/"):
                values.add(relative)
        return values
    for value in (*policy.read_sources,*policy.shared_sources):
        exact.update(variants(value))
    for value in policy.read_roots:
        roots.update(variants(value))
    exact.update(roots)
    clauses=[];params=[]
    if exact:
        clauses.append("path IN ("+",".join("?" for _ in exact)+")")
        params.extend(sorted(exact))
    for root in sorted(roots):
        prefix=root.rstrip("/")+"/"
        clauses.append("(path>=? AND path<?)")
        params.extend((prefix,prefix[:-1]+"0"))
    # Legacy spellings are screened natively; never authorize from this prefilter.
    clauses.append("(path GLOB '*//*' OR path GLOB './*' OR path GLOB '*/./*')")
    return " OR ".join(clauses),params

def install_admission(conn, policy, path_mapper=lambda p: p, *, indexed_candidates=False, include_telemetry=True):
    """Classify metadata ONCE; pin main DB snapshot before payload retrieval."""
    if not isinstance(policy,AdmissionPolicy):
        raise ValueError("verified admission policy required")
    if conn.execute("SELECT 1 FROM sqlite_temp_master WHERE name IN "
                    "('nodes','edges','embeddings','capacity_admitted_ids')").fetchone():
        raise RuntimeError("admission requires a fresh connection")
    # Classification and payload/edge reads share one WAL snapshot. Concurrent
    # ingest cannot replace a classified node's metadata/body underneath a query.
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("CREATE TEMP TABLE capacity_admitted_ids(id TEXT PRIMARY KEY) WITHOUT ROWID")
    where,params=_candidate_sql(policy) if indexed_candidates else ("1",[])
    from .telemetry import is_telemetry
    admitted=[]
    for row in conn.execute("SELECT id,path,metadata FROM main.nodes WHERE "+where,params):
        try:
            if policy.allows(path_mapper(row[1]),row[2]) and (include_telemetry or not is_telemetry(row[2])):
                admitted.append((row[0],))
        except (TypeError,ValueError,AttributeError):
            pass
    conn.executemany("INSERT OR IGNORE INTO capacity_admitted_ids(id) VALUES (?)",admitted)
    # Indexed admitted IDs gate every later payload lookup; no repeated UDF.
    conn.execute("CREATE TEMP VIEW nodes AS SELECT n.* FROM main.nodes n "
                 "WHERE n.id IN (SELECT id FROM capacity_admitted_ids)")
    for table,sql in (
        ("edges","SELECT e.* FROM main.edges e WHERE "
                 "e.from_id IN (SELECT id FROM capacity_admitted_ids) AND "
                 "e.to_id IN (SELECT id FROM capacity_admitted_ids)"),
        ("embeddings","SELECT e.* FROM main.embeddings e WHERE "
                      "e.node_id IN (SELECT id FROM capacity_admitted_ids)"),
    ):
        if conn.execute("SELECT 1 FROM main.sqlite_master WHERE name=?",(table,)).fetchone():
            conn.execute("CREATE TEMP VIEW "+table+" AS "+sql)
    conn.execute("PRAGMA query_only=ON")

# Only declared, admitted assignments are authority; caller paths never are.
from pathlib import Path
import hashlib
from . import config as C

REGISTRY_PATH = C.UNIVERSE_ROOT / "08 🚀 PROJECTS/0700_INDEX/project_status_registry.json"
PERSONAL_ASSIGNMENTS_PATH = C.UNIVERSE_ROOT / "08 🚀 PROJECTS/0700_INDEX/personal_brain_assignments.json"
SHARED_SOURCES = tuple(str(p) for p in (
    C.DESKTOP_ROOT / "COVENANT/00 🛡 Governance/Master Commandments.md",
    C.DESKTOP_ROOT / "COVENANT/00 🛡 Governance/Operating Card.md",
    C.UNIVERSE_ROOT / "08 🚀 PROJECTS/0700_INDEX/HQ_SESSION_CONTRACT.md",
    C.UNIVERSE_ROOT / "08 🚀 PROJECTS/0700_INDEX/HQ_COMPANY_VIEW.md",
    C.UNIVERSE_ROOT / "03 🛰️ AGENTS/0400_INDEX/OFFICE_WINDOWS.md",
))

BUSINESS_GOVERNING_SOURCES = (str(C.UNIVERSE_ROOT / "00 ⚖️ GOVERNANCE/0205_MEMORY_GOVERNANCE/BRAIN_COOWNERSHIP.md"),)

def load_admission(capacity="business", project_id=None):
    if capacity not in ("business", "personal"):
        raise ValueError("capacity must be business or personal")
    if project_id is not None and (not isinstance(project_id,str) or not project_id):
        raise ValueError("project_id must identify an admitted assignment")
    manifest = REGISTRY_PATH if capacity == "business" else PERSONAL_ASSIGNMENTS_PATH
    try:
        records = json.loads(manifest.read_text())
        if not isinstance(records,list) or any(not isinstance(r,dict) for r in records):
            raise ValueError("assignment manifest must contain an array of records")
    except (OSError,ValueError,TypeError) as exc:
        raise ValueError(f"{capacity} admission manifest unavailable or malformed: {manifest}; "
                         "restore the trusted assignment manifest before retrieval") from exc
    flag = "hq_admitted" if capacity == "business" else "assignment_admitted"
    admitted = [r for r in records if r.get("capacity") == capacity and r.get(flag) is True]
    if project_id is not None:
        admitted = [r for r in admitted if r.get("id") == project_id]
    if not admitted:
        raise ValueError(f"no admitted {capacity} assignment for project_id={project_id!r}; "
                         "request a classified assignment with declared read_roots/read_sources")
    roots, sources = [], []
    ids = set()
    for row in admitted:
        if not isinstance(row.get("id"),str) or not row["id"] or row["id"] in ids:
            raise ValueError("admission manifest has invalid or duplicate assignment IDs")
        ids.add(row["id"])
        for key,target in (("read_roots", roots),("read_sources",sources)):
            values = row.get(key,[])
            if values is None: values=[]
            if not isinstance(values,list):
                raise ValueError(f"admitted assignment {row['id']} has malformed {key}")
            target.extend(normalized_path(p) for p in values)
        if not (row.get("read_roots") or row.get("read_sources")):
            raise ValueError(f"admitted assignment {row['id']} has no declared read scope")
    return AdmissionPolicy(capacity,tuple(sorted(set(roots))),SHARED_SOURCES + (BUSINESS_GOVERNING_SOURCES if capacity == "business" else ()),
                           tuple(sorted(set(sources))))

def policy_key(policy):
    return hashlib.sha256(json.dumps({
        "capacity":policy.capacity,"roots":policy.read_roots,
        "sources":policy.read_sources,"shared":policy.shared_sources},
        sort_keys=True).encode()).hexdigest()

def stored_source_path(stored):
    # Reject traversal BEFORE source_path maps old relative storage formats.
    if not isinstance(stored,str) or not stored or ".." in Path(stored).parts or "\\" in stored:
        raise ValueError("invalid source path")
    from .source_integrity import source_path
    path=source_path(stored)
    if path is None: raise ValueError("non-file source has no assigned path")
    return path.as_posix()

# Applies to the configured graph and source inventory.
import os
STRICT_BUSINESS_UNION = os.environ.get("ARCTURION_BRAIN_STRICT_BUSINESS_UNION", "") == "1"

def domain_neutral_default(capacity, project_id):
    return capacity == "business" and project_id is None and not STRICT_BUSINESS_UNION

def _declared_personal_scope():
    """Personal read scope declared anywhere; excluded from the domain-neutral default."""
    roots, sources = [], []
    for manifest in (REGISTRY_PATH, PERSONAL_ASSIGNMENTS_PATH):
        try:
            records = json.loads(manifest.read_text())
        except FileNotFoundError:
            if manifest == PERSONAL_ASSIGNMENTS_PATH:
                continue  # not yet authored: nothing declared personal there
            raise ValueError(f"business admission manifest unavailable: {manifest}")
        except (OSError, ValueError) as exc:
            raise ValueError(f"admission manifest unreadable or malformed: {manifest}; "
                             "restore it before retrieval") from exc
        if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
            raise ValueError(f"assignment manifest must contain an array of records: {manifest}")
        for r in records:
            if r.get("capacity") != "personal":
                continue
            for key, target in (("read_roots", roots), ("read_sources", sources)):
                values = r.get(key) or []
                if not isinstance(values, list):
                    raise ValueError(f"personal assignment {r.get('id')!r} has malformed {key}")
                target.extend(normalized_path(v) for v in values)
    return tuple(sorted(set(roots))), tuple(sorted(set(sources)))

def _label_sql(key):
    # Business and shared (both-worlds) labels are admissible; personal/polarion and any
    # unknown or non-text label are contrary. Lazily evaluated CASE: invalid JSON never
    # reaches json_type/json_extract.
    return (f"(CASE WHEN metadata IS NULL THEN NULL WHEN json_valid(metadata) THEN "
            f"CASE WHEN json_type(metadata,'$.{key}') IS NULL THEN NULL "
            f"WHEN json_type(metadata,'$.{key}')='text' THEN "
            f"CASE lower(json_extract(metadata,'$.{key}')) WHEN 'business' THEN 'business' "
            f"WHEN 'arcturion' THEN 'business' WHEN 'shared' THEN 'business' "
            f"ELSE '!' END ELSE '!' END ELSE '!' END)")

def install_domain_neutral(conn, *, include_telemetry=False):
    """Domain-neutral default scope. Same temp-view contract as install_admission."""
    if conn.execute("SELECT 1 FROM sqlite_temp_master WHERE name IN "
                    "('nodes','edges','embeddings','capacity_admitted_ids')").fetchone():
        raise RuntimeError("admission requires a fresh connection")
    roots, sources = _declared_personal_scope()
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("CREATE TEMP TABLE capacity_admitted_ids(id TEXT PRIMARY KEY) WITHOUT ROWID")
    where = [
        "(metadata IS NULL OR (json_valid(metadata) AND json_type(metadata)='object'))",
        f"coalesce({_label_sql('capacity')},'business')='business'",
        f"coalesce({_label_sql('world')},'business')='business'",
    ]
    params = []
    if not include_telemetry:
        where.append("(metadata IS NULL OR NOT json_valid(metadata) OR "
                     "json_extract(metadata,'$.record_class') IS NOT 'machine_telemetry')")
    if roots or sources:
        # Conservative storage-path superset of the declared personal scope.
        personal_sql, personal_params = _candidate_sql(AdmissionPolicy("personal", roots, (), sources))
        where.append("NOT (" + personal_sql + ")")
        params.extend(personal_params)
    conn.execute("INSERT INTO capacity_admitted_ids SELECT id FROM main.nodes WHERE "
                 + " AND ".join(where), params)
    conn.execute("CREATE TEMP VIEW nodes AS SELECT n.* FROM main.nodes n "
                 "WHERE n.id IN (SELECT id FROM capacity_admitted_ids)")
    # No edges view here: every query-path edge read gates both endpoints through `nodes`,
    # edges carry no source payload, and an IN-filtered edges view over ~100k admitted ids
    # turns a 2-hop walk into a multi-minute scan.
    if conn.execute("SELECT 1 FROM main.sqlite_master WHERE name='embeddings'").fetchone():
        conn.execute("CREATE TEMP VIEW embeddings AS SELECT e.* FROM main.embeddings e "
                     "WHERE e.node_id IN (SELECT id FROM capacity_admitted_ids)")
    conn.execute("PRAGMA query_only=ON")

def retrieval_connection(capacity="business", project_id=None, *, include_telemetry=False):
    """Retrieval entry (query, search, receipts): domain-neutral default per Cmd 044."""
    return scoped_connection(capacity, project_id, include_telemetry=include_telemetry,
                             domain_neutral=True)

def scoped_connection(capacity="business",project_id=None, *, include_telemetry=False,
                      domain_neutral=False):
    """Strict registry/assignment scope unless the caller opts into the retrieval default."""
    from . import db as DB
    if not isinstance(include_telemetry, bool):
        raise ValueError("include_telemetry must be boolean")
    if domain_neutral and domain_neutral_default(capacity, project_id):
        key = "business:domain-neutral" + (":evidence" if include_telemetry else ":knowledge")
        conn = DB.connect()
        try:
            install_domain_neutral(conn, include_telemetry=include_telemetry)
            conn.create_function("capacity_scope_key",0,lambda:key,deterministic=True)
            return conn
        except Exception:
            conn.close()
            raise
    policy=load_admission(capacity,project_id)
    key=policy_key(policy) + (":evidence" if include_telemetry else ":knowledge")
    conn=DB.connect()
    try:
        install_admission(conn,policy,stored_source_path,indexed_candidates=True,include_telemetry=include_telemetry)
        conn.create_function("capacity_scope_key",0,lambda:key,deterministic=True)
        return conn
    except Exception:
        conn.close()
        raise

def is_scoped(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_temp_master WHERE type='view' AND name='nodes'").fetchone())

def scope_key(conn):
    return conn.execute("SELECT capacity_scope_key()").fetchone()[0] if is_scoped(conn) else "unscoped"

# Loaded once, so health reports the code this process imported, not a later disk edit.
_LOADED_HASHES = {name: hashlib.sha256((Path(__file__).parent/name).read_bytes()).hexdigest()
                  for name in ("capacity_admission.py","brain.py","http_server.py",
                               "mcp_server.py","vector_seed.py","rank_signals.py")}

def deployment_status():
    return {"default_capacity":"business", "gate":"before-payload",
            "default_business_scope": "strict-union" if STRICT_BUSINESS_UNION else "domain-neutral",
            "loaded_source_sha256":dict(_LOADED_HASHES)}

def retrieval_coverage_warning(capacity, project_id=None):
    return coverage_warning(capacity, project_id, domain_neutral=True)

def coverage_warning(capacity,project_id=None, *, domain_neutral=False):
    if domain_neutral and domain_neutral_default(capacity, project_id):
        return ("capacity_scope:business; assignment:domain-neutral-default (Cmd 044: the Brain "
                "indexes both worlds); contrary labels, declared personal assignments and machine "
                "telemetry excluded; unclassified sources included; pass project_id for strict scope")
    return (f"capacity_scope:{capacity}; assignment:{project_id or 'admitted-union'}; "
            "outside declared assignments, contrary labels and unclassified sources excluded; coverage is partial")
