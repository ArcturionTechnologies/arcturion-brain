"""Isolated, evidence-bound index for the versioned VEGA desk pack.

Only explicit callers write. The automatic hooks are separately opt-in. These
tables are deliberately outside nodes/edges/FTS: ordinary graph maintenance and
traversal must never encounter desk-only concepts or predicates.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path, PurePosixPath

from . import config as C

PACK_PATH = Path(__file__).resolve().parent.parent / "canon" / "vega_desk_v1.json"
ENABLE_MARKER = PACK_PATH.parent / "vega_desk.enabled"
DECLARATION_ROOTS = (
    "VEGA/000 📈 VEGA/Knowledge",
    "VEGA/02 📈 Strategies & Playbooks",
    "ATLAS/12. 📈 Trading & Markets HQ/📐 Setups & Taxonomy",
    "ATLAS/12. 📈 Trading & Markets HQ/📊 Signals & Correlations",
    "ATLAS/12. 📈 Trading & Markets HQ/🌍 Market Regime",
    "ATLAS/12. 📈 Trading & Markets HQ/🛡 Risk Protocol",
    "ATLAS/12. 📈 Trading & Markets HQ/🧪 Backtests",
    "ATLAS/12. 📈 Trading & Markets HQ/📚 Education",
    "VEGA",
)
EMPIRICAL = frozenset({"paper", "backtest"})
KINDS = frozenset({"doctrine", "definition", "hypothesis", "catalog", *EMPIRICAL})
SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
PROTECTED_COMPONENTS = ("journal", "account", "credential", "secret", "archive", "logs", "founder", "memory")

SCHEMA = (
    "CREATE TABLE IF NOT EXISTS vega_desk_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS vega_desk_concepts (id TEXT PRIMARY KEY, label TEXT NOT NULL, kind TEXT NOT NULL, area TEXT NOT NULL, definition TEXT NOT NULL, aliases_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS vega_desk_predicates (id TEXT PRIMARY KEY, label TEXT NOT NULL, from_kinds_json TEXT NOT NULL, to_kinds_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS vega_desk_evidence (id TEXT PRIMARY KEY, source_path TEXT NOT NULL, source_node_id TEXT, quote TEXT NOT NULL, start_offset INTEGER, end_offset INTEGER, source_hash TEXT)",
    "CREATE TABLE IF NOT EXISTS vega_desk_edges (id TEXT PRIMARY KEY, from_id TEXT NOT NULL, to_id TEXT NOT NULL, predicate TEXT NOT NULL, kind TEXT NOT NULL, context_json TEXT NOT NULL, evidence_id TEXT NOT NULL, status TEXT NOT NULL, fingerprint TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS vega_desk_review_queue (edge_id TEXT PRIMARY KEY, reason TEXT NOT NULL, resolution TEXT, reviewed_fingerprint TEXT, note TEXT)",
    "CREATE TABLE IF NOT EXISTS vega_desk_claim_origin (edge_id TEXT PRIMARY KEY, origin TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS vega_desk_literal_blocks (signature TEXT PRIMARY KEY)",
    "CREATE INDEX IF NOT EXISTS idx_vega_evidence_source ON vega_desk_evidence(source_path)",
    "CREATE INDEX IF NOT EXISTS idx_vega_edges_from ON vega_desk_edges(from_id)",
    "CREATE INDEX IF NOT EXISTS idx_vega_edges_to ON vega_desk_edges(to_id)",
    "CREATE INDEX IF NOT EXISTS idx_vega_edges_evidence ON vega_desk_edges(evidence_id)",
)


class PackError(ValueError):
    """A structurally invalid pack cannot replace a good existing index."""


def enabled():
    """Read activation each time; explicit environment setting overrides marker."""
    flag = os.environ.get("ARCTURION_VEGA_DESK")
    if flag is not None:
        return flag == "1"
    return ENABLE_MARKER.is_file()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _strings(value, name, *, nonempty=False):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value) or (nonempty and not value):
        raise PackError(f"{name} must be a list of nonempty strings")
    return value


def _string(obj, key):
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PackError(f"{key} must be a nonempty string")
    return value


def _source_path(value):
    if not isinstance(value, str) or "\\" in value or "\x00" in value:
        raise PackError("invalid source path")
    p = PurePosixPath(value)
    if (p.is_absolute() or len(p.parts) < 2 or p.parts[0] not in {"VEGA", "ATLAS"}
            or any(part in {".", ".."} for part in value.split("/"))
            or any(word in part.casefold() for part in p.parts[1:-1] for word in PROTECTED_COMPONENTS)
            or re.match(r"^(?:trade[ _-]*)?(?:journal|account|credentials?|secrets?)(?:[ ._-]|$)", p.name.casefold())
            or p.as_posix() != value):
        raise PackError(f"source path must be a normalized VEGA/ or ATLAS/ path: {value!r}")
    return value


def validate_pack(pack):
    """Validate the desk vocabulary without extending the global ontology."""
    if not isinstance(pack, dict) or type(pack.get("version")) is not int or pack["version"] != 1:
        raise PackError("pack version must be 1")
    for key in ("concepts", "predicates", "rules", "gaps"):
        if not isinstance(pack.get(key), list):
            raise PackError(f"{key} must be a list")
    _strings(pack["gaps"], "gaps")
    concepts = {}
    for item in pack["concepts"]:
        if not isinstance(item, dict):
            raise PackError("concept must be an object")
        slug = _string(item, "id")
        if not SLUG.fullmatch(slug) or slug in concepts:
            raise PackError(f"invalid or duplicate concept id: {slug}")
        for key in ("label", "kind", "area", "definition"):
            _string(item, key)
        _strings(item.get("aliases"), "aliases")
        concepts[slug] = item
    predicates = {}
    for item in pack["predicates"]:
        if not isinstance(item, dict):
            raise PackError("predicate must be an object")
        slug = _string(item, "id")
        if not SLUG.fullmatch(slug) or not slug.startswith("vega_") or slug in predicates:
            raise PackError(f"invalid or duplicate predicate id: {slug}")
        _string(item, "label")
        for key in ("from_kinds", "to_kinds"):
            _strings(item.get(key), key, nonempty=True)
        predicates[slug] = item
    seen = set()
    for rule in pack["rules"]:
        if not isinstance(rule, dict):
            raise PackError("rule must be an object")
        for flag in ("inferred", "ambiguous", "conflicting"):
            if flag in rule and type(rule[flag]) is not bool:
                raise PackError(f"{flag} must be a boolean")
        _source_path(rule.get("source"))
        _string(rule, "quote")
        subject, obj, pred, kind = (_string(rule, k) for k in ("from", "to", "predicate", "kind"))
        if subject not in concepts or obj not in concepts or pred not in predicates or kind not in KINDS:
            raise PackError("unknown endpoint, predicate, or kind")
        spec = predicates[pred]
        if concepts[subject]["kind"] not in spec["from_kinds"] or concepts[obj]["kind"] not in spec["to_kinds"]:
            raise PackError("predicate domain/range mismatch")
        context = rule.get("context", {})
        if not isinstance(context, dict):
            raise PackError("context must be an object")
        if kind in EMPIRICAL and any(not isinstance(context.get(k), str) or not context[k].strip() for k in ("instrument", "horizon", "sample_window")):
            raise PackError("paper and backtest require instrument, horizon, and sample_window")
        identity = _rule_id(rule)
        if identity in seen:
            raise PackError("duplicate rule")
        seen.add(identity)
    return concepts, predicates


def _rule_id(rule):
    # Source and exact quote are part of identity: independent passages survive
    # independent deletion. Inferred/review flags do not alter semantic identity.
    return "vde_" + _hash(_json({k: rule.get(k, {} if k == "context" else None) for k in ("source", "quote", "from", "to", "predicate", "kind", "context")}))


def _literal_signature(source, quote, subject, predicate, target):
    return _hash(_json([source, quote, subject, predicate, target]))


def _absolute_source(root, relative):
    root = Path(root).resolve()
    _source_path(relative)
    target = root
    # Reject every symlink component, including links within VEGA/ATLAS that
    # could relabel a private document as an approved market source.
    for part in PurePosixPath(relative).parts:
        target = target / part
        if target.is_symlink():
            raise PackError(f"symlink source refused: {relative}")
    target = target.resolve()
    if not target.is_relative_to(root / PurePosixPath(relative).parts[0]):
        raise PackError(f"source escapes permitted root: {relative}")
    return target


def _source_node(conn, source):
    # Exactly one canonical document is required; ambiguous or absent means no
    # permission-bearing node, so retrieval must exclude it.
    try:
        rows = conn.execute("SELECT id FROM nodes WHERE path=? LIMIT 2", (source,)).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return None
    return rows[0][0] if len(rows) == 1 else None


def _inspect(root, rule):
    try:
        path = _absolute_source(root, rule["source"])
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeError, PackError):
        return None, None, None, "source_unavailable"
    digest = hashlib.sha256(raw).hexdigest()
    quote = rule["quote"]
    # A declaration cannot cite its own frontmatter as supporting evidence.
    offset = 0
    if rule.get("_declaration") or rule.get("_body_assertion"):
        from .util import parse_frontmatter
        _, body = parse_frontmatter(text)
        offset = len(text) - len(body)
        if rule.get("_declaration") and not offset:
            return digest, None, None, "quote_missing"
    start = text.find(quote, offset)
    if start < 0:
        return digest, None, None, "quote_missing"
    if text.find(quote, start + 1) >= 0:
        return digest, start, start + len(quote), "ambiguous_passage"
    return digest, start, start + len(quote), None


def _selected_paths(paths, root):
    if paths is None:
        return None
    base = Path(root).resolve()
    selected = set()
    for item in paths:
        p = Path(item)
        if p.is_absolute():
            try:
                name = p.resolve().relative_to(base).as_posix()
            except ValueError:
                continue
        else:
            name = p.as_posix()
        # An explicitly watched directory includes all descendants, including
        # deleted sources whose path no longer exists.
        if name in {"VEGA", "ATLAS"}:
            selected.add(name)
        else:
            try:
                selected.add(_source_path(name))
            except PackError:
                continue
    return selected


def _matches(path, selected):
    return selected is None or any(path == p or path.startswith(p + "/") for p in selected)


def _vega_body_allowed(source, *, is_dir=False):
    """Apply the corpus classification before any VEGA declaration body read."""
    parts = PurePosixPath(source).parts
    if not parts or parts[0] != "VEGA":
        return True
    from . import vega_corpus
    return all(vega_corpus._classify(parts[:end], end < len(parts) or is_dir) is None
               for end in range(2, len(parts) + 1))


def _safe_declaration_files(base, selected_path):
    """Walk bounded declaration shelves without entering excluded directories."""
    from . import vega_corpus
    pending = [selected_path]
    while pending:
        path = pending.pop()
        try:
            if path.is_symlink():
                continue
            rel = path.relative_to(base).as_posix()
            is_dir = path.is_dir()
            if rel != "VEGA":
                _absolute_source(base, rel if not is_dir else rel + "/__scope__.md")
            if rel == "VEGA" or rel.startswith("VEGA/"):
                if not _vega_body_allowed(rel, is_dir=is_dir):
                    continue
            else:
                parts = PurePosixPath(rel).parts
                if any(vega_corpus._PRUNE.match(p) or p.startswith(".") or vega_corpus._PRIVATE_FILE.search(p) for p in parts[1:]):
                    continue
            if is_dir:
                pending.extend(sorted(path.iterdir(), reverse=True))
            elif path.is_file() and path.suffix.lower() == ".md" and path.stat().st_size <= vega_corpus._MAX_BYTES:
                yield path
        except (OSError, ValueError):
            continue


def _literal_assertions(text, source, pack):
    """Match only a complete literal `concept predicate concept` body line."""
    concepts = {}
    for c in pack["concepts"]:
        for label in [c["label"], *c["aliases"]]:
            concepts.setdefault(label.casefold(), set()).add(c["id"])
    predicates = {}
    for p in pack["predicates"]:
        predicates.setdefault(p["label"].casefold(), []).append(p["id"])
    from .util import parse_frontmatter
    _, body = parse_frontmatter(text)
    out, errors = [], []
    fenced = False
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or not line or line.startswith(("#", ">")) or re.search(r"\b(?:unless|if|when|may|might|could|would|should)\b", line, re.I):
            continue
        statement = re.sub(r"^[-*] +", "", line).rstrip(".!?").strip().casefold()
        # Questions are not assertions, even if the words otherwise match.
        if line.endswith("?"):
            continue
        for label, pred_ids in predicates.items():
            delimiter = " " + label + " "
            negative = False
            # A negative predicate label is itself an affirmative registered
            # relationship; never invert its embedded 'not'. For positive
            # labels, check negation first because it contains the plain verb.
            negative_delimiters = []
            if not re.search(r"\b(?:not|never)\b", label):
                words = label.split(" ")
                first = words[0]
                rest = " ".join(words[1:])
                suffix = (" " + rest) if rest else ""
                negative_delimiters.extend([" not " + label + " ", " never " + label + " "])
                if first in {"is", "are", "has", "have"}:
                    negative_delimiters.extend([" " + first + " not" + suffix + " ", " " + first + " never" + suffix + " "])
                    if first in {"has", "have"}:
                        negative_delimiters.append(" does not have" + suffix + " ")
                elif first.endswith("s") and len(first) > 1:
                    negative_delimiters.append(" does not " + first[:-1] + suffix + " ")
            for negative_delimiter in negative_delimiters:
                if statement.count(negative_delimiter) == 1:
                    delimiter, negative = negative_delimiter, True
                    break
            if statement.count(delimiter) != 1:
                continue
            left, right = statement.split(delimiter, 1)
            left_ids, right_ids = concepts.get(left, set()), concepts.get(right, set())
            if not left_ids or not right_ids:
                continue
            if len(left_ids) * len(right_ids) * len(pred_ids) > 8:
                errors.append(("vdi_" + _hash(_json([source, line])), f"ambiguous_assertion:{source}:{line}"))
                continue
            ambiguous = len(left_ids) * len(right_ids) * len(pred_ids) != 1
            for subject in sorted(left_ids):
                for target in sorted(right_ids):
                    for pred in pred_ids:
                        rule = {"source": source, "quote": line, "from": subject, "to": target, "predicate": pred, "kind": "hypothesis", "_body_assertion": True}
                        if ambiguous:
                            rule["ambiguous"] = True
                        if negative:
                            rule["conflicting"] = True
                            rule["_negative"] = True
                        try:
                            validate_pack({**pack, "rules": [rule]})
                        except PackError:
                            continue
                        out.append(rule)
    return out, errors


def _declarations(root, selected, pack):
    """Explicit YAML or strict literal assertions in approved shelves only."""
    from .util import parse_frontmatter
    rules, errors = [], []
    base = Path(root).resolve()
    # Traverse VEGA once with the shared corpus classifier; ATLAS remains
    # restricted to the six explicitly approved shelves.
    shelves = [s for s in DECLARATION_ROOTS if s.startswith("ATLAS/")] + ["VEGA"]
    for shelf in shelves:
        directory = base / shelf
        if not directory.is_dir() or directory.is_symlink():
            continue
        if selected is None:
            candidates = _safe_declaration_files(base, directory)
        else:
            candidates = []
            for item in selected:
                if shelf == item or shelf.startswith(item + "/"):
                    candidates.extend(_safe_declaration_files(base, directory))
                elif item.startswith(shelf + "/"):
                    candidate = base / item
                    candidates.extend(_safe_declaration_files(base, candidate))
        for path in candidates:
            try:
                rel = path.relative_to(base).as_posix()
                try:
                    _source_path(rel)
                    _absolute_source(base, rel)
                except PackError:
                    continue
                if not _vega_body_allowed(rel):
                    continue
                if not _matches(rel, selected) or path.is_symlink() or not path.resolve().is_relative_to(directory.resolve()):
                    continue
                text = path.read_text(encoding="utf-8")
                literals, literal_errors = _literal_assertions(text, rel, pack)
                errors.extend(literal_errors)
                fm, body = parse_frontmatter(text)
                if text.startswith("---") and "vega_edges" in text[:65536] and not fm:
                    raise PackError("invalid frontmatter containing vega_edges")
                if body == text or "vega_edges" not in fm:
                    rules.extend(literals)
                    continue
                values = fm["vega_edges"]
                if not isinstance(values, list):
                    raise PackError("vega_edges must be a list")
                declared = []
                invalid = False
                for number, item in enumerate(values):
                    try:
                        if not isinstance(item, dict) or "source" in item:
                            raise PackError("declaration must be a source-local object")
                        rule = {**item, "source": rel, "_declaration": True}
                        validate_pack({**pack, "rules": [rule]})
                        declared.append(rule)
                    except PackError as exc:
                        invalid = True
                        errors.append(("vdi_" + _hash(_json([rel, number])), f"invalid_declaration:{rel}:{number}:{exc}"))
                rules.extend(declared)
                # An explicit declaration owns its passage and interpretation.
                # Invalid declarations suppress implicit parsing for the whole
                # document so malformed flags cannot bypass the review gate.
                if not invalid:
                    keys = {(r["quote"], r["from"], r["predicate"], r["to"]) for r in declared}
                    rules.extend(r for r in literals if (r["quote"], r["from"], r["predicate"], r["to"]) not in keys)
            except (OSError, UnicodeError, PackError) as exc:
                errors.append(("vdi_" + _hash(rel), f"unreadable_declaration:{rel}:{type(exc).__name__}"))
    return rules, errors


def sync(conn, *, paths=None, root=None, pack_path=None):
    """Scan sources first, then reserve the writer only to apply the diff.

    A caller transaction is never taken over. Database inputs read during the
    unlocked scan are re-checked under the reservation; if another writer
    changed them, the plan is rebuilt while holding the lock.
    """
    options = {"paths": paths, "root": root, "pack_path": pack_path}
    owned = not conn.in_transaction
    plan = _prepare_sync(conn, **options)
    if owned:
        conn.execute("BEGIN IMMEDIATE")
    try:
        result = _apply_prepared_sync(conn, plan, options)
        if owned and result.get("unchanged"):
            conn.rollback()  # no writes: do not leave an unnecessary reservation
        return result
    except Exception:
        if owned:
            conn.rollback()
        raise


def _optional_rows(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return []


def _desk_db_inputs(conn, requested):
    """Every database read that shapes the scan plan, for re-checking under lock."""
    old = _optional_rows(conn, "SELECT value FROM vega_desk_meta WHERE key='pack_sha256'")
    prior_conflict = bool(_optional_rows(conn, "SELECT 1 FROM vega_desk_review_queue WHERE reason LIKE '%explicit_conflict%' LIMIT 1"))
    touched = None
    if requested is not None:
        rows = _optional_rows(conn, "SELECT DISTINCT source_path FROM vega_desk_evidence")
        touched = any(_matches(row[0], requested) for row in rows)
    return (old[0][0] if old else None, prior_conflict, touched)


def _inspect_key(rule):
    return (rule["source"], rule["quote"], bool(rule.get("_declaration")), bool(rule.get("_body_assertion")))


def _prepare_sync(conn, *, paths=None, root=None, pack_path=None):
    """All filesystem reads and hashing for a desk sync; performs no writes."""
    root = Path(root) if root is not None else C.DESKTOP_ROOT
    pack_path = Path(pack_path) if pack_path is not None else PACK_PATH
    try:
        pack_bytes = pack_path.read_bytes()
        pack = json.loads(pack_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PackError(f"cannot load pack: {exc}") from exc
    from .validator import validate_desk_pack
    concepts, predicates = validate_desk_pack("VEGA", pack)
    requested = _selected_paths(paths, root)
    pack_hash = hashlib.sha256(pack_bytes).hexdigest()
    db_inputs = _desk_db_inputs(conn, requested)
    old_hash, prior_conflict, touched = db_inputs
    plan = {"requested": requested, "db_inputs": db_inputs}
    selected = requested
    if old_hash != pack_hash:
        selected = None  # Changed seed pack needs complete reconciliation.
    declarations, declaration_errors = _declarations(root, selected, pack)
    if selected is not None and not declarations and not declaration_errors and not any(_matches(r["source"], selected) for r in pack["rules"]):
        if not touched:
            plan["result"] = {"rules_processed": 0, "unchanged": True}
            return plan
    # Explicit contradiction is bilateral: revisit existing counterparts as well
    # as the new assertion, even when the incoming event is incremental.
    if selected is not None and (prior_conflict or any(r.get("conflicting") is True for r in [*pack["rules"], *declarations])):
        selected = None
        declarations, declaration_errors = _declarations(root, None, pack)
    pack = {**pack, "rules": [*pack["rules"], *declarations]}
    # Read and hash every candidate passage now, so the write reservation
    # only covers database work.
    inspected, source_literals = {}, {}
    for r in pack["rules"]:
        if not _matches(r["source"], selected):
            continue
        key = _inspect_key(r)
        if key not in inspected:
            inspected[key] = _inspect(root, r)
        if r.get("_declaration") and r["source"] not in source_literals:
            try:
                body_text = _absolute_source(root, r["source"]).read_text(encoding="utf-8")
                source_literals[r["source"]] = _literal_assertions(body_text, r["source"], pack)[0]
            except (OSError, UnicodeError, PackError):
                source_literals[r["source"]] = []
    plan.update(root=root, pack=pack, pack_hash=pack_hash, concepts=concepts, predicates=predicates,
                selected=selected, declaration_errors=declaration_errors,
                inspected=inspected, source_literals=source_literals)
    return plan


def _apply_prepared_sync(conn, plan, options):
    """Apply a prepared plan; rebuild it (under the caller's lock) if its inputs moved."""
    if _desk_db_inputs(conn, plan["requested"]) != plan["db_inputs"]:
        plan = _prepare_sync(conn, **options)
    return _apply_sync(conn, plan)


def _sync_locked(conn, *, paths=None, root=None, pack_path=None):
    """Atomically reconcile a pack into isolated tables on the supplied connection.

    paths=None reconciles every source and removes deleted rules. A path list
    only reconciles those sources (directories include descendants). Caller owns
    the transaction; no connection, live DB, or commit is opened here.
    """
    return _apply_sync(conn, _prepare_sync(conn, paths=paths, root=root, pack_path=pack_path))


def _apply_sync(conn, plan):
    """Database-only half of a desk sync: no source file is opened here."""
    if "result" in plan:
        return dict(plan["result"])
    pack, concepts, predicates = plan["pack"], plan["concepts"], plan["predicates"]
    selected, declaration_errors, pack_hash = plan["selected"], plan["declaration_errors"], plan["pack_hash"]
    inspected, source_literals = plan["inspected"], plan["source_literals"]
    pack = {**pack, "rules": list(pack["rules"])}
    conn.execute("SAVEPOINT vega_desk_sync")
    try:
        for ddl in SCHEMA:
            conn.execute(ddl)
        # A reviewed declaration cannot be resurrected as an implicit literal
        # simply by removing or breaking its frontmatter.
        reviewed = conn.execute("SELECT v.source_path,v.quote,e.from_id,e.predicate,e.to_id FROM vega_desk_edges e JOIN vega_desk_evidence v ON v.id=e.evidence_id JOIN vega_desk_claim_origin o ON o.edge_id=e.id JOIN vega_desk_review_queue q ON q.edge_id=e.id WHERE o.origin='declaration'").fetchall()
        for row in reviewed:
            conn.execute("INSERT OR IGNORE INTO vega_desk_literal_blocks VALUES (?)", (_literal_signature(*row),))
        blocked = {row[0] for row in conn.execute("SELECT signature FROM vega_desk_literal_blocks")}
        pack["rules"] = [r for r in pack["rules"] if not r.get("_body_assertion") or _literal_signature(r["source"], r["quote"], r["from"], r["predicate"], r["to"]) not in blocked]
        # Invalid declarations are visible for review, never materialized as
        # retrievable edges. Rebuild this small diagnostic set on full scans.
        if selected is None:
            conn.execute("DELETE FROM vega_desk_review_queue WHERE edge_id LIKE 'vdi_%'")
        else:
            for eid, reason in conn.execute("SELECT edge_id,reason FROM vega_desk_review_queue WHERE edge_id LIKE 'vdi_%'").fetchall():
                parts = reason.split(":", 2)
                if len(parts) >= 2 and _matches(parts[1], selected):
                    conn.execute("DELETE FROM vega_desk_review_queue WHERE edge_id=?", (eid,))
        for eid, reason in declaration_errors:
            conn.execute("INSERT INTO vega_desk_review_queue(edge_id,reason) VALUES (?,?) ON CONFLICT(edge_id) DO UPDATE SET reason=excluded.reason,resolution=NULL,reviewed_fingerprint=NULL,note=NULL", (eid, reason))
        for c in concepts.values():
            conn.execute("INSERT INTO vega_desk_concepts VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label,kind=excluded.kind,area=excluded.area,definition=excluded.definition,aliases_json=excluded.aliases_json", (c["id"], c["label"], c["kind"], c["area"], c["definition"], _json(c["aliases"])))
        for p in predicates.values():
            conn.execute("INSERT INTO vega_desk_predicates VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label,from_kinds_json=excluded.from_kinds_json,to_kinds_json=excluded.to_kinds_json", (p["id"], p["label"], _json(p["from_kinds"]), _json(p["to_kinds"])))
        conflict_keys = {(r["from"], r["predicate"], _json(r.get("context", {}))) for r in pack["rules"] if r.get("conflicting") is True}
        active = set()
        for r in pack["rules"]:
            if not _matches(r["source"], selected):
                continue
            rid = _rule_id(r)
            active.add(rid)
            digest, start, end, problem = inspected[_inspect_key(r)]
            eid = "vds_" + _hash(_json([r["source"], r["quote"]]))
            node_id = _source_node(conn, r["source"])
            reasons = []
            if problem:
                reasons.append(problem)
            if not node_id:
                reasons.append("source_node_unresolved")
            if any(r.get(flag) is True for flag in ("inferred", "ambiguous")):
                reasons.append("declared_uncertain")
            if r.get("_declaration"):
                direct = source_literals[r["source"]]
                if not any(x["quote"] == r["quote"] and x["from"] == r["from"] and x["to"] == r["to"] and x["predicate"] == r["predicate"] and not x.get("_negative") and not x.get("ambiguous") for x in direct):
                    reasons.append("semantic_review_required")
            if (r["from"], r["predicate"], _json(r.get("context", {}))) in conflict_keys:
                reasons.append("explicit_conflict")
            if r.get("_negative") is True:
                reasons.append("negative_assertion")
            fingerprint = _hash(_json([rid, digest, start, end, node_id, sorted(reasons)]))
            prior = conn.execute("SELECT resolution,reviewed_fingerprint,reason FROM vega_desk_review_queue WHERE edge_id=?", (rid,)).fetchone()
            previous_edge = conn.execute("SELECT fingerprint FROM vega_desk_edges WHERE id=?", (rid,)).fetchone()
            resolution = prior[0] if prior and prior[1] == fingerprint else None
            if prior and ((previous_edge and previous_edge[0] != fingerprint) or (not resolution and "review_changed" in prior[2].split(","))):
                reasons.append("review_changed")
            elif prior and resolution and not reasons:
                reasons.append("reviewed")
            status = "rejected" if resolution == "rejected" else ("accepted" if resolution == "accepted" or not reasons else "queued")
            conn.execute("INSERT INTO vega_desk_evidence VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET source_path=excluded.source_path,source_node_id=excluded.source_node_id,quote=excluded.quote,start_offset=excluded.start_offset,end_offset=excluded.end_offset,source_hash=excluded.source_hash", (eid, r["source"], node_id, r["quote"], start, end, digest))
            conn.execute("INSERT INTO vega_desk_edges VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET from_id=excluded.from_id,to_id=excluded.to_id,predicate=excluded.predicate,kind=excluded.kind,context_json=excluded.context_json,evidence_id=excluded.evidence_id,status=excluded.status,fingerprint=excluded.fingerprint", (rid, r["from"], r["to"], r["predicate"], r["kind"], _json(r.get("context", {})), eid, status, fingerprint))
            origin = "declaration" if r.get("_declaration") else ("literal" if r.get("_body_assertion") else "seed")
            conn.execute("INSERT INTO vega_desk_claim_origin VALUES (?,?) ON CONFLICT(edge_id) DO UPDATE SET origin=excluded.origin", (rid, origin))
            if reasons:
                conn.execute("INSERT INTO vega_desk_review_queue (edge_id,reason,resolution,reviewed_fingerprint) VALUES (?,?,?,?) ON CONFLICT(edge_id) DO UPDATE SET reason=excluded.reason,resolution=excluded.resolution,reviewed_fingerprint=excluded.reviewed_fingerprint,note=CASE WHEN vega_desk_review_queue.reviewed_fingerprint=excluded.reviewed_fingerprint THEN vega_desk_review_queue.note ELSE NULL END", (rid, ",".join(sorted(reasons)), resolution, fingerprint if resolution else None))
            elif prior:
                conn.execute("DELETE FROM vega_desk_review_queue WHERE edge_id=?", (rid,))
        existing = conn.execute("SELECT e.id,v.source_path FROM vega_desk_edges e JOIN vega_desk_evidence v ON v.id=e.evidence_id LEFT JOIN vega_desk_claim_origin o ON o.edge_id=e.id WHERE COALESCE(o.origin,'seed')!='corpus'").fetchall()
        for edge_id, source in existing:
            if _matches(source, selected) and edge_id not in active:
                conn.execute("DELETE FROM vega_desk_review_queue WHERE edge_id=?", (edge_id,))
                conn.execute("DELETE FROM vega_desk_edges WHERE id=?", (edge_id,))
                conn.execute("DELETE FROM vega_desk_claim_origin WHERE edge_id=?", (edge_id,))
        conn.execute("DELETE FROM vega_desk_evidence WHERE id NOT IN (SELECT evidence_id FROM vega_desk_edges)")
        # Keep vocab referenced by untouched incremental edges until full sync.
        if selected is None:
            for table, keep in (("vega_desk_concepts", concepts), ("vega_desk_predicates", predicates)):
                for (old,) in conn.execute(f"SELECT id FROM {table}").fetchall():
                    if table == "vega_desk_concepts":
                        referenced = conn.execute("SELECT 1 FROM vega_desk_edges WHERE from_id=? OR to_id=? LIMIT 1", (old, old)).fetchone()
                    else:
                        referenced = conn.execute("SELECT 1 FROM vega_desk_edges WHERE predicate=? LIMIT 1", (old,)).fetchone()
                    if old not in keep and not referenced:
                        conn.execute(f"DELETE FROM {table} WHERE id=?", (old,))
            conn.execute("INSERT INTO vega_desk_meta(key,value) VALUES ('pack_sha256',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (pack_hash,))
        counts = {"concepts": len(concepts), "predicates": len(predicates), "rules_processed": len(active), "queued": conn.execute("SELECT COUNT(*) FROM vega_desk_edges WHERE status='queued'").fetchone()[0]}
        conn.execute("RELEASE vega_desk_sync")
        return counts
    except Exception:
        conn.execute("ROLLBACK TO vega_desk_sync")
        conn.execute("RELEASE vega_desk_sync")
        raise


def resolve_review(conn, edge_id, resolution, *, note, expected_fingerprint, root=None, pack_path=None, rules_path=None):
    """Reserve before reading review state, preserving any caller transaction."""
    owned = not conn.in_transaction
    if owned:
        conn.execute("BEGIN IMMEDIATE")
    try:
        return _resolve_review_locked(conn, edge_id, resolution, note=note, expected_fingerprint=expected_fingerprint, root=root, pack_path=pack_path, rules_path=rules_path)
    except Exception:
        if owned:
            conn.rollback()
        raise


def _resolve_review_locked(conn, edge_id, resolution, *, note, expected_fingerprint, root=None, pack_path=None, rules_path=None):
    """Record a decision for current evidence; subsequent sync revalidates it."""
    if resolution not in {"accepted", "rejected"} or not isinstance(note, str) or not note.strip():
        raise ValueError("resolution must be accepted/rejected with a nonempty note")
    source = conn.execute("SELECT v.source_path,COALESCE(o.origin,'seed') FROM vega_desk_edges e JOIN vega_desk_evidence v ON v.id=e.evidence_id LEFT JOIN vega_desk_claim_origin o ON o.edge_id=e.id WHERE e.id=?", (edge_id,)).fetchone()
    if source:
        if source[1] == "corpus":
            from .vega_corpus_index import sync_corpus
            sync_corpus(conn, paths=[source[0]], root=root, pack_path=pack_path, rules_path=rules_path)
        else:
            sync(conn, paths=[source[0]], root=root, pack_path=pack_path)
    row = conn.execute("SELECT e.fingerprint,q.reason FROM vega_desk_edges e JOIN vega_desk_review_queue q ON q.edge_id=e.id WHERE e.id=?", (edge_id,)).fetchone()
    if not row:
        raise ValueError("edge is not in review queue")
    if not expected_fingerprint or row[0] != expected_fingerprint:
        raise ValueError("evidence changed since review list; inspect the updated entry")
    if resolution == "accepted" and any(x in row[1].split(",") for x in ("source_unavailable", "quote_missing", "source_node_unresolved", "negative_assertion", "source_changed_during_scan")):
        raise ValueError("source and exact passage must be resolved before acceptance")
    conn.execute("UPDATE vega_desk_review_queue SET resolution=?,reviewed_fingerprint=?,note=? WHERE edge_id=?", (resolution, row[0], note, edge_id))
    conn.execute("UPDATE vega_desk_edges SET status=? WHERE id=?", (resolution, edge_id))
    declaration = conn.execute("SELECT v.source_path,v.quote,e.from_id,e.predicate,e.to_id FROM vega_desk_edges e JOIN vega_desk_evidence v ON v.id=e.evidence_id JOIN vega_desk_claim_origin o ON o.edge_id=e.id WHERE e.id=? AND o.origin='declaration'", (edge_id,)).fetchone()
    if declaration:
        conn.execute("INSERT OR IGNORE INTO vega_desk_literal_blocks VALUES (?)", (_literal_signature(*declaration),))


def list_reviews(conn):
    """List reviewable assertions and their exact candidate source passage."""
    rows = conn.execute("SELECT q.edge_id,q.reason,q.resolution,q.note,e.from_id,e.predicate,e.to_id,e.kind,e.context_json,v.source_path,v.quote,e.fingerprint FROM vega_desk_review_queue q LEFT JOIN vega_desk_edges e ON e.id=q.edge_id LEFT JOIN vega_desk_evidence v ON v.id=e.evidence_id ORDER BY q.edge_id").fetchall()
    keys = ("edge_id", "reason", "resolution", "note", "from", "predicate", "to", "kind", "context_json", "source_path", "quote", "fingerprint")
    return [dict(zip(keys, row)) for row in rows]


def export_reviews(conn):
    """Portable decision receipt; caller chooses where to store it safely."""
    rows = conn.execute("SELECT edge_id,resolution,reviewed_fingerprint,note FROM vega_desk_review_queue WHERE resolution IN ('accepted','rejected') ORDER BY edge_id").fetchall()
    blocks = {row[0] for row in conn.execute("SELECT signature FROM vega_desk_literal_blocks")}
    pending = conn.execute("SELECT v.source_path,v.quote,e.from_id,e.predicate,e.to_id FROM vega_desk_edges e JOIN vega_desk_evidence v ON v.id=e.evidence_id JOIN vega_desk_claim_origin o ON o.edge_id=e.id JOIN vega_desk_review_queue q ON q.edge_id=e.id WHERE o.origin='declaration'").fetchall()
    blocks.update(_literal_signature(*row) for row in pending)
    return {"version": 1, "decisions": [dict(zip(("edge_id", "resolution", "fingerprint", "note"), row)) for row in rows], "literal_blocks": sorted(blocks)}


def import_reviews(conn, receipt):
    """Restore only decisions bound to the exact currently indexed evidence."""
    if not isinstance(receipt, dict) or receipt.get("version") != 1 or not isinstance(receipt.get("decisions"), list):
        raise ValueError("invalid review receipt")
    blocks = receipt.get("literal_blocks", [])
    if not isinstance(blocks, list) or any(not isinstance(x, str) or not re.fullmatch(r"[a-f0-9]{64}", x) for x in blocks):
        raise ValueError("invalid literal-block receipt")
    for signature in blocks:
        conn.execute("INSERT OR IGNORE INTO vega_desk_literal_blocks VALUES (?)", (signature,))
    if blocks:
        rows = conn.execute("SELECT e.id,v.source_path,v.quote,e.from_id,e.predicate,e.to_id FROM vega_desk_edges e JOIN vega_desk_evidence v ON v.id=e.evidence_id JOIN vega_desk_claim_origin o ON o.edge_id=e.id WHERE o.origin='literal'").fetchall()
        for edge_id, *parts in rows:
            if _literal_signature(*parts) in blocks:
                conn.execute("DELETE FROM vega_desk_review_queue WHERE edge_id=?", (edge_id,))
                conn.execute("DELETE FROM vega_desk_claim_origin WHERE edge_id=?", (edge_id,))
                conn.execute("DELETE FROM vega_desk_edges WHERE id=?", (edge_id,))
        conn.execute("DELETE FROM vega_desk_evidence WHERE id NOT IN (SELECT evidence_id FROM vega_desk_edges)")
    restored = skipped = 0
    for decision in receipt["decisions"]:
        if not isinstance(decision, dict):
            skipped += 1
            continue
        row = conn.execute("SELECT e.fingerprint,q.reason FROM vega_desk_edges e JOIN vega_desk_review_queue q ON q.edge_id=e.id WHERE e.id=?", (decision.get("edge_id"),)).fetchone()
        value, note = decision.get("resolution"), decision.get("note")
        if (not row or row[0] != decision.get("fingerprint") or value not in {"accepted", "rejected"}
                or not isinstance(note, str) or not note.strip()
                or (value == "accepted" and any(x in row[1].split(",") for x in ("source_unavailable", "quote_missing", "source_node_unresolved", "negative_assertion", "source_changed_during_scan")))):
            skipped += 1
            continue
        conn.execute("UPDATE vega_desk_review_queue SET resolution=?,reviewed_fingerprint=?,note=? WHERE edge_id=?", (value, row[0], note, decision["edge_id"]))
        conn.execute("UPDATE vega_desk_edges SET status=? WHERE id=?", (value, decision["edge_id"]))
        restored += 1
    return {"restored": restored, "skipped": skipped}


def auto_sync(conn, *, paths=None):
    """Production integration is disabled unless the exact flag is supplied."""
    if not enabled():
        return None
    from .vega_corpus_index import apply_prepared_corpus, prepare_corpus
    owned = not conn.in_transaction
    options = {"paths": paths}
    # The slow part (walking and hashing the VEGA workspace) runs before the
    # writer reservation so other Brain writers are not starved; the locked
    # section below only re-checks plan inputs and applies the diff.
    desk_plan = _prepare_sync(conn, **options)
    corpus_plan = prepare_corpus(conn, **options)
    if owned:
        conn.execute("BEGIN IMMEDIATE")
    conn.execute("SAVEPOINT vega_desk_auto")
    try:
        result = {"desk": _apply_prepared_sync(conn, desk_plan, options),
                  "corpus": apply_prepared_corpus(conn, corpus_plan, options)}
        conn.execute("RELEASE vega_desk_auto")
        if owned and result["desk"].get("unchanged") and result["corpus"].get("unchanged"):
            conn.rollback()
        return result
    except Exception:
        conn.execute("ROLLBACK TO vega_desk_auto")
        conn.execute("RELEASE vega_desk_auto")
        if owned:
            conn.rollback()
        raise


def main(argv=None):
    """Bounded operator workflow; an explicit database path is always required."""
    import argparse
    parser = argparse.ArgumentParser(description="VEGA desk review and receipt tools")
    parser.add_argument("--db", required=True, help="existing SQLite index path")
    parser.add_argument("--root", type=Path, default=C.DESKTOP_ROOT)
    parser.add_argument("--pack", type=Path, default=PACK_PATH)
    parser.add_argument("--rules", type=Path, help="absolute path to caller-owned optional corpus rules JSON")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    sub.add_parser("sync", help="rebuild only the isolated desk tables")
    sub.add_parser("coverage", help="report workspace inventory and visibility counts")
    review = sub.add_parser("review")
    review.add_argument("edge_id")
    review.add_argument("resolution", choices=("accepted", "rejected"))
    review.add_argument("--note", required=True)
    review.add_argument("--expected-fingerprint", required=True)
    export = sub.add_parser("export-reviews")
    export.add_argument("file", type=Path)
    restore = sub.add_parser("import-reviews")
    restore.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    if not Path(args.db).is_file():
        parser.error("database must already exist")
    conn = sqlite3.connect(f"file:{Path(args.db).resolve().as_posix()}?mode=rw", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        if args.action in {"sync", "review", "import-reviews"}:
            conn.execute("BEGIN IMMEDIATE")
        if args.action == "list":
            result = list_reviews(conn)
        elif args.action == "sync":
            from .vega_corpus_index import sync_corpus
            result = {"desk": sync(conn, root=args.root, pack_path=args.pack), "corpus": sync_corpus(conn, root=args.root, pack_path=args.pack, rules_path=args.rules)}
        elif args.action == "coverage":
            from .vega_corpus_index import coverage
            result = coverage(conn)
            try:
                from .capacity_admission import install_domain_neutral
                from .vega_query import coverage_stats
                install_domain_neutral(conn)
                result["query_visible"] = coverage_stats(conn=conn)
                result.pop("visibility_note", None)
            except Exception as exc:
                result["visibility_note"] = f"Scoped visibility unavailable: {type(exc).__name__}"
        elif args.action == "review":
            resolve_review(conn, args.edge_id, args.resolution, note=args.note, expected_fingerprint=args.expected_fingerprint, root=args.root, pack_path=args.pack, rules_path=args.rules)
            result = {"edge_id": args.edge_id, "resolution": args.resolution}
        elif args.action == "export-reviews":
            # Exclusive creation prevents accidentally overwriting a receipt.
            with args.file.open("x", encoding="utf-8") as handle:
                json.dump(export_reviews(conn), handle, ensure_ascii=False, indent=2)
            result = {"saved": str(args.file)}
        else:
            sync(conn, root=args.root, pack_path=args.pack)
            from .vega_corpus_index import sync_corpus
            sync_corpus(conn, root=args.root, pack_path=args.pack, rules_path=args.rules)
            result = import_reviews(conn, json.loads(args.file.read_text(encoding="utf-8")))
        conn.commit()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
