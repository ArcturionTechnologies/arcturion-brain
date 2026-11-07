"""Permission-first retrieval of the separately indexed VEGA desk claims."""
from __future__ import annotations

import json
import re
import hashlib
import sqlite3
from datetime import datetime, timezone

from . import brain as B
from . import vega_corpus
from . import config as C
from .vega_index import PACK_PATH, _absolute_source, _source_path
from .source_integrity import filter_retrieval, privacy_reason

_STOP = frozenset("a an and are as at be by can could did do does for from how i in is it its me of on or tell that the this to was what when where which who why with you".split())
_QUESTION_FILLER = frozenset("about after an any applies apply before can could did do does for given had happened has have how in include is it its make must my of on or place please relate rule the this to use uses using versus was what when where which who why with would your".split())
_UNKNOWN = re.compile(r"\b(?:tomorrow|next (?:year|month|week)|will|unnamed|win rate|profitable|profitability|average return|net profit|exact|percentage|percent|commission|threshold|how many|how much|last\b.{0,40}\b(?:fill|roll))\b", re.I)
_PENDING_REVIEW = re.compile(r"(?mi)^\s*(?:>\s*)?sync_status:\s*['\"]?pending_review\b")
_RETIRED_BANNER = re.compile(r"(?mi)^\s*>[^\n]*\b(?:RETIRED|ARCHIVED)\b")
_ORB_INTENTS = (
    (r"\b(?:stop|stops)\b", "vega_places_stop_at"),
    (r"\b(?:kill|kills|invalidation|invalidate|invalidated|fails|failure|fakeout)\b", "vega_invalidated_by"),
    (r"\b(?:confirm|confirms|confirmed|confirmation)\b", "vega_requires_confirmation_from"),
    (r"\b(?:skip|avoids?|stand aside|stands aside|sit out|sitting out|no trade)\b", "vega_stands_aside_when"),
    (r"\b(?:trigger|triggers|triggered|entry|enter)\b", "vega_triggers_on"),
)
_RELATION_INTENTS = (
    (r"\bstops?\b", r"(?:stop|stops)"),
    (r"\bvalidat(?:ion|ed|e)\b", r"validat"),
    (r"\bprox(?:y|ies)\b", r"prox(?:y|ies)"),
    (r"\bcomponents?\b", r"component"),
    (r"\broll(?:s|ing)?(?:\s+effects?)?\b", r"(?:adjustment|roll)"),
    (r"\bcompare\b", r"(?:must_exceed|greater_than|less_than)"),
)


def _relation_patterns(topic, predicate, label):
    """Recognized relation cues only; endpoint checks still gate directness."""
    relation = predicate + " " + label
    return [question for question, candidate in _RELATION_INTENTS
            if re.search(question, topic, re.I) and re.search(candidate, relation, re.I)]


def _tokens(text):
    return {word for word in re.findall(r"[a-z0-9]+", str(text).lower()) if word not in _STOP}


def _phrases(concept):
    try:
        aliases = json.loads(concept["aliases_json"] or "[]")
    except (TypeError, ValueError):
        aliases = []
    if not isinstance(aliases, list):
        aliases = []
    return [str(x).lower() for x in [concept["label"], concept["id"], *aliases]
            if isinstance(x, str) and x.strip()]


def _source_lifecycle_allowed(node, now):
    status = str(node.get("status") or "").lower()
    if re.search(r"\b(?:withdrawn|superseded|expired)\b", status) or node.get("superseded_by"):
        return False
    for field in ("valid_from", "valid_until"):
        raw = node.get(field)
        if not raw:
            continue
        parsed = B._parse_ts(raw)
        if parsed is None or (field == "valid_from" and parsed > now) or (field == "valid_until" and parsed <= now):
            return False
    return True


def _verify_evidence(row, source_node, files, *, reject_retired=False):
    """Validate an already-admitted indexed passage against its current source."""
    if privacy_reason({"path": row["source_path"], "body_excerpt": row["quote"]}):
        return False
    try:
        relative = _source_path(row["source_path"])
        path = _absolute_source(C.DESKTOP_ROOT, relative)
        if relative not in files:
            raw = path.read_bytes()
            files[relative] = (hashlib.sha256(raw).hexdigest(), raw.decode("utf-8"))
        digest, contents = files[relative]
        start, end = row["start_offset"], row["end_offset"]
        if (digest != row["source_hash"] or type(start) is not int or type(end) is not int
                or start < 0 or end < start or contents[start:end] != row["quote"]):
            return False
        row["absolute_path"] = str(path)
        row["line"] = contents.count("\n", 0, start) + 1
        row["source_review"] = ("pending_review" if relative.startswith("ATLAS/") and _PENDING_REVIEW.search(contents) else None)
        status = str(source_node.get("status") or "")
        row["source_retired"] = bool(re.search(r"\b(?:retired|archived)\b", status, re.I)
                                     or _RETIRED_BANNER.search(contents[:4000]))
        return not (reject_retired and row["source_retired"])
    except (OSError, UnicodeError, ValueError):
        return False


def _require_current_pack(conn):
    try:
        current = hashlib.sha256(PACK_PATH.read_bytes()).hexdigest()
        indexed = conn.execute("SELECT value FROM main.vega_desk_meta WHERE key='pack_sha256'").fetchone()
    except (OSError, sqlite3.Error) as exc:
        raise ValueError("VEGA desk pack unavailable; reindex before querying") from exc
    if indexed is None or indexed[0] != current:
        raise ValueError("VEGA desk pack changed; reindex before querying")
    corpus_meta = dict(conn.execute("SELECT key,value FROM main.vega_desk_meta WHERE key IN ('corpus_pack_sha256','corpus_version')"))
    try:
        from .vega_corpus_index import _rules_input, corpus_version
        rule_config, _ = _rules_input(None)
        expected_version = corpus_version(rule_config)
    except (OSError, UnicodeError, ValueError, TypeError, re.error):
        return False
    return (corpus_meta.get("corpus_pack_sha256") == current
            and corpus_meta.get("corpus_version") == expected_version)


def _corpus_row_allowed(row, current):
    origin = row["claim_origin"]
    path = row["source_path"]
    if origin == "corpus":
        return current and vega_corpus.admitted_markdown_path(path)
    if origin in {"declaration", "literal"} and path.startswith("VEGA/"):
        return vega_corpus.admitted_markdown_path(path)
    return True



def coverage_stats(*, conn=None, capacity="business", project_id=None):
    """Count currently query-visible navigation and substantive sources and edges.

    A supplied connection must already carry the trusted capacity admission view;
    its scope, not the optional arguments, determines the result. This explicit
    reporting operation reads admitted sources to check current versions.
    """
    from .capacity_admission import is_scoped
    owned = conn is None
    if owned:
        conn = B.scoped_connection(capacity, project_id, include_telemetry=False)
    try:
        if not is_scoped(conn):
            raise ValueError("coverage requires a trusted scoped connection")
        corpus_current = _require_current_pack(conn)
        joins = """ FROM main.vega_desk_edges e
          LEFT JOIN main.vega_desk_claim_origin o ON o.edge_id=e.id
          JOIN main.vega_desk_evidence v ON v.id=e.evidence_id
          JOIN main.vega_desk_concepts a ON a.id=e.from_id
          JOIN main.vega_desk_concepts z ON z.id=e.to_id
          JOIN main.vega_desk_predicates p ON p.id=e.predicate
          JOIN nodes n ON n.id=v.source_node_id AND n.path=v.source_path AND n.content_hash=v.source_hash
          WHERE e.status='accepted' AND v.source_hash IS NOT NULL AND v.source_hash != ''"""
        rows = {r["id"]: dict(r) for r in conn.execute("SELECT DISTINCT n.*" + joins)}
        now = datetime.now(timezone.utc)
        permitted = {n["id"] for n in filter_retrieval(conn, list(rows.values())) if _source_lifecycle_allowed(n, now)}
        files = {}
        matched = {"navigation": set(), "substantive": set(), "corpus": set()}
        edges = {"navigation": 0, "substantive": 0, "corpus": 0}
        for raw in conn.execute("SELECT e.id AS edge_id,n.id AS node_id,e.kind,coalesce(o.origin,'unclassified') AS claim_origin,e.context_json,v.source_path,v.quote,v.start_offset,v.end_offset,v.source_hash" + joins):
            row = dict(raw)
            if row["node_id"] not in permitted or not _corpus_row_allowed(row, corpus_current):
                continue
            try:
                if not isinstance(json.loads(row["context_json"] or "{}"), dict):
                    continue
            except (TypeError, ValueError):
                continue
            if _verify_evidence(row, rows[row["node_id"]], files):
                category = "navigation" if row["kind"] == "catalog" else "substantive"
                edges[category] += 1
                matched[category].add(row["node_id"])
                if row["claim_origin"] == "corpus":
                    edges["corpus"] += 1
                    matched["corpus"].add(row["node_id"])
        return {"corpus_index_current": corpus_current,
                "query_visible_navigation_sources": len(matched["navigation"]),
                "query_visible_navigation_assertions": edges["navigation"],
                "query_visible_substantive_sources": len(matched["substantive"]),
                "query_visible_substantive_assertions": edges["substantive"],
                "query_visible_corpus_sources": len(matched["corpus"]),
                "query_visible_corpus_edges": edges["corpus"]}
    finally:
        if owned:
            conn.close()


def _concept_score(concept, topic, terms):
    if concept["id"] == "vega_greek" and not terms.intersection({"option", "options", "greek", "greeks", "volatility"}):
        return 0
    score = 0
    normalized = " ".join(re.findall(r"[a-z0-9]+", topic.lower()))
    for phrase in _phrases(concept):
        phrase = " ".join(re.findall(r"[a-z0-9]+", phrase))
        words = _tokens(phrase)
        if not words:
            continue
        # Whole labels and aliases win over generic overlapping words. Boundaries
        # prevent e.g. ORB from matching an unrelated longer word.
        exact = bool(re.search(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", normalized))
        overlap = len(words & terms)
        unordered = overlap >= 2 and overlap / len(words) >= 0.6
        # Exact multiword subjects must outrank partial overlaps and generic
        # aliases, regardless of whether the evidence is seed or reviewed corpus.
        score = max(score, (200 + 15 * len(words) + 5 * overlap) if exact else
                    (40 + 15 * overlap if unordered else 0))
    return score


class DeskPack(B.ContextPack):
    def __init__(self, *args, claims=None, answer_status="unknown", unmatched_terms=None, paths=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.claims = claims or []
        self.answer_status = answer_status
        self.unmatched_terms = unmatched_terms or []
        self.paths = paths or []

    def to_dict(self):
        return {**super().to_dict(), "desk": "VEGA", "claims": self.claims, "edges": self.edges,
                "answer_status": self.answer_status, "unmatched_terms": self.unmatched_terms,
                "paths": self.paths}


def _grounding_words(text):
    # A simple word check is deliberately only a reason to withhold an answer;
    # it is not proof that a passage semantically answers a question.
    return {w for w in re.findall(r"[a-z0-9]+", str(text).lower())
            if len(w) > 2 and w not in _QUESTION_FILLER}


def query_desk(topic, *, token_budget=2000, capacity="business", project_id=None, predicates=None, desk_hops=None):
    if not isinstance(topic, str) or not topic.strip():
        raise ValueError("VEGA desk requires a topic")
    if isinstance(token_budget, bool) or not isinstance(token_budget, int) or not 100 <= token_budget <= 8000:
        raise ValueError("VEGA desk budget must be an integer from 100 to 8000")
    if predicates is not None and (not isinstance(predicates, (list, tuple)) or
            not predicates or any(not isinstance(p, str) or not p for p in predicates)):
        raise ValueError("VEGA desk predicates must be a nonempty list of predicate IDs")
    if desk_hops is not None and (type(desk_hops) is not int or not 1 <= desk_hops <= 3):
        raise ValueError("desk_hops must be an integer from 1 to 3")
    path_mode = desk_hops is not None and desk_hops > 1
    unknown = bool(_UNKNOWN.search(topic))
    comparison_text = re.sub(r"\bperpetual[- ]versus[- ]spot\b", "perpetual spot", topic, flags=re.I)
    comparison = bool(re.search(r"\b(?:differ|difference|versus|compare|comparison|vs)\b", comparison_text, re.I))
    orb_intent = next((predicate for pattern, predicate in _ORB_INTENTS if re.search(pattern, topic, re.I)), None) if re.search(r"\b(?:orb|opening.range.breakout)\b", topic, re.I) else None
    if re.search(r"\bfunding\b", topic, re.I) and re.search(r"\b(?:pay|pays|payer|payment|receiver|receives)\b", topic, re.I):
        orb_intent = "vega_funding_payment"
    conn = B.scoped_connection(capacity, project_id, include_telemetry=False)
    try:
        corpus_current = _require_current_pack(conn)
        # This first pass reads only scoped source nodes and opaque edge IDs. Exact
        # path and content hash bind each claim to the same indexed source version.
        # The existing filter additionally applies privacy, quarantine and memory policy.
        candidates = conn.execute("""
            SELECT DISTINCT n.* FROM main.vega_desk_edges e
            JOIN main.vega_desk_evidence v ON v.id=e.evidence_id
            JOIN nodes n ON n.id=v.source_node_id AND n.path=v.source_path
                        AND n.content_hash=v.source_hash
            WHERE v.source_hash IS NOT NULL AND v.source_hash != '' AND e.status='accepted'
        """).fetchall()
        source_rows = {r["id"]: dict(r) for r in candidates}
        now = datetime.now(timezone.utc)
        permitted = {r["id"] for r in filter_retrieval(conn, list(source_rows.values()))
                     if _source_lifecycle_allowed(r, now)}
        claims = []
        catalog = []
        planned_paths = []
        paths_capped = False
        ranking_pruned = False
        terms = _tokens(topic)
        files = {}
        verified = {}
        def verify(item):
            row, a, z, source_id = item
            if row["id"] in verified:
                return verified[row["id"]]
            ok = _verify_evidence(row, source_rows[source_id], files,
                                  reject_retired=bool(path_mode or re.search(r"\b(?:active|current|operative|live)\b", topic, re.I)))
            verified[row["id"]] = ok
            return ok
        def admitted_records():
            # Batching avoids one SQL round trip per edge as the desk grows.
            ids = sorted(permitted)
            for start in range(0, len(ids), 400):
                batch = ids[start:start + 400]
                placeholders = ",".join("?" for _ in batch)
                sql = """
              SELECT e.*, v.source_path, v.quote, v.start_offset, v.end_offset, v.source_hash,
                     n.id AS admitted_source_id,
                     coalesce(o.origin, 'unclassified') AS claim_origin,
                     a.id AS a_id, a.label AS a_label, a.kind AS a_kind, a.area AS a_area,
                     a.aliases_json AS a_aliases_json,
                     z.id AS z_id, z.label AS z_label, z.kind AS z_kind, z.area AS z_area,
                     z.aliases_json AS z_aliases_json, p.label AS predicate_label
              FROM main.vega_desk_edges e
              JOIN main.vega_desk_evidence v ON v.id=e.evidence_id
              JOIN main.vega_desk_concepts a ON a.id=e.from_id
              JOIN main.vega_desk_concepts z ON z.id=e.to_id
              JOIN main.vega_desk_predicates p ON p.id=e.predicate
              LEFT JOIN main.vega_desk_claim_origin o ON o.edge_id=e.id
              JOIN nodes n ON n.id=v.source_node_id AND n.path=v.source_path
                          AND n.content_hash=v.source_hash
              WHERE e.status='accepted' AND n.id IN (""" + placeholders + ")"
                yield from conn.execute(sql, batch)
        for row in (() if unknown else admitted_records()):
            row = dict(row)
            if not _corpus_row_allowed(row, corpus_current):
                continue
            source_id = row["admitted_source_id"]
            a = {k: row["a_" + k] for k in ("id", "label", "kind", "area", "aliases_json")}
            z = {k: row["z_" + k] for k in ("id", "label", "kind", "area", "aliases_json")}
            try:
                if not isinstance(json.loads(row["context_json"] or "{}"), dict):
                    continue
            except (TypeError, ValueError):
                continue
            catalog.append((row, a, z, source_id))
            if path_mode:
                continue
            if ((predicates is not None and row["predicate"] not in predicates)
                    or (orb_intent and row["predicate"] != orb_intent)):
                continue
            a_score, z_score = _concept_score(a, topic, terms), _concept_score(z, topic, terms)
            if (re.search(r"\broll(?:s|ing)?\b", topic, re.I) and
                    not re.search(r"\broll(?:s|ing)?\b", " ".join((a["label"], z["label"], row["predicate_label"], row["quote"], row["context_json"])), re.I)):
                continue
            score = max(a_score, z_score)
            if not score:
                continue
            if not verify((row, a, z, source_id)):
                continue
            matches = {c["id"] for c, s in ((a, a_score), (z, z_score)) if s}
            claims.append((score, row, a, z, source_id, matches))
        if path_mode:
            # Exact IDs take precedence over aliases. Only concepts attached to
            # already admitted edges participate in seed selection or traversal.
            norm = lambda s: " ".join(re.findall(r"[a-z0-9]+", s.lower()))
            concepts = {c["id"]: c for _, a, z, _ in catalog for c in (a, z)}
            seed = [c["id"] for c in concepts.values() if c["id"] == topic.strip()]
            if not seed:
                seed = [c["id"] for c in concepts.values() if norm(topic) in {norm(x) for x in _phrases(c)}]
            if len(seed) > 1:
                raise ValueError("VEGA desk path subject is ambiguous; use a concept ID")
            by_from = {}
            for item in catalog:
                by_from.setdefault(item[1]["id"], []).append(item)
            for values in by_from.values():
                origin_priority = {"seed": 0, "declaration": 1, "literal": 2, "corpus": 3, "unclassified": 4}
                values.sort(key=lambda item: (item[0]["kind"] == "catalog", origin_priority.get(item[0]["claim_origin"], 4), item[0]["id"]))
            def walk(node, seen, steps):
                nonlocal paths_capped
                if len(planned_paths) >= 12:
                    paths_capped = True
                    return
                if len(steps) >= desk_hops:
                    planned_paths.append(tuple(steps))
                    return
                options = [item for item in by_from.get(node, [])
                           if item[2]["id"] not in seen
                           and (steps or predicates is None or item[0]["predicate"] in predicates)
                           and verify(item)]
                if not options:
                    if steps:
                        planned_paths.append(tuple(steps))
                    return
                for index, item in enumerate(options):
                    walk(item[2]["id"], seen | {item[2]["id"]}, steps + [item[0]["id"]])
                    if len(planned_paths) >= 12:
                        if index < len(options) - 1:
                            paths_capped = True
                        break
            if seed:
                walk(seed[0], {seed[0]}, [])
            by_id = {item[0]["id"]: item for item in catalog}
            ordered = list(dict.fromkeys(edge for path in planned_paths for edge in path))
            claims = [(len(ordered)-i, *by_id[eid], set()) for i, eid in enumerate(ordered)]
        if not path_mode:
            origin_priority = {"seed": 0, "declaration": 1, "literal": 2, "corpus": 3, "unclassified": 4}
            relevance = lambda item: item[0] + (60 if _relation_patterns(topic, item[1]["predicate"], item[1]["predicate_label"]) else 0)
            claims.sort(key=lambda item: (item[1]["kind"] == "catalog", -relevance(item), origin_priority.get(item[1]["claim_origin"], 4), item[1]["id"]))
            previous_count = len(claims)
            substantive = [item for item in claims if item[1]["kind"] != "catalog"]
            navigation_request = bool(re.search(r"\b(?:documents?|files?|sources?|shelves|shelf|where (?:is|are).{0,30}(?:filed|stored))\b", topic, re.I))
            if substantive and predicates is None and not navigation_request:
                claims = substantive
            if substantive and not comparison and predicates is None and not navigation_request:
                best = max(relevance(item) for item in substantive)
                claims = [item for item in claims if relevance(item) >= best - 35]
            ranking_pruned = len(claims) < previous_count
        if comparison and len(set().union(*(item[5] for item in claims))) < 2:
            claims = []
        coverage = B.coverage_warning(capacity, project_id)
        warnings = [coverage, "Desk evidence was checked against the current source bytes at query time."]
        if not corpus_current:
            warnings.append("VEGA workspace backfill needs reindexing. Only base desk evidence is available.")
        rendered = []
        selected = []
        sources = {}
        grounding_text = []
        direct_seed = []
        relation_cues = []
        used = 0
        structured_estimate = 0
        for _, row, a, z, source_id, _ in claims:
            try:
                context = json.loads(row["context_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(context, dict):
                continue
            claim = {"id": row["id"], "from": {k:a[k] for k in ("id", "label", "kind", "area")},
                     "predicate": row["predicate"], "predicate_label": row["predicate_label"],
                     "to": {k:z[k] for k in ("id", "label", "kind", "area")},
                     "kind": row["kind"], "status": row["status"], "origin": row["claim_origin"], "context": context,
                     "evidence": {"path": row["source_path"], "source_node_id": source_id,
                                  "absolute_path": row["absolute_path"], "line": row["line"],
                                  "source_status": source_rows[source_id].get("status"),
                                  "source_review": row["source_review"],
                                  "source_retired": row["source_retired"],
                                  "source_hash": row["source_hash"], "start_offset": row["start_offset"],
                                  "end_offset": row["end_offset"],
                                  "offset_unit": "unicode_codepoint", "end_exclusive": True,
                                  "quote": row["quote"]}}
            line = (f"- {a['label']} — {row['predicate_label']} — {z['label']} "
                    f"[index status: {row['status']}; kind: {row['kind']}]\n"
                    f"  Evidence: [source](<{row['absolute_path']}>) line {row['line']} "
                    f"(Unicode offsets {row['start_offset']}–{row['end_offset']}, end exclusive; "
                    f"SHA-256 {row['source_hash']})\n  > {row['quote']}"
                    + ("\n  Source note: ATLAS projection pending review; index acceptance is not source approval." if row["source_review"] else "")
                    + ("\n  Source note: RETIRED historical reference; do not treat this rule as active." if row["source_retired"] else "")
                    + ("\n  Catalog navigation only; this link is not strategy, performance or backtest evidence." if row["kind"] == "catalog" else "")
                    + ("\n  Context: " + json.dumps(context, ensure_ascii=False, sort_keys=True) if context else ""))
            cost = max(1, (len(line) + 3) // 4)
            if used + cost > token_budget:
                continue
            # MCP's limit applies to serialized JSON. Each claim is repeated
            # in edges and sometimes paths, with the citation also in text.
            copies = 2 + sum(row["id"] in path for path in planned_paths)
            descriptor = {key: source_rows[source_id].get(key) for key in ("id", "path", "content_hash")}
            estimate = (copies * len(json.dumps(claim).encode("utf-8"))
                        + len(json.dumps(line).encode("utf-8"))
                        + len(json.dumps(descriptor).encode("utf-8"))
                        + len(json.dumps(row["source_path"]).encode("utf-8")) + 300)
            if structured_estimate + estimate > 22000:
                continue
            structured_estimate += estimate
            used += cost
            rendered.append(line)
            selected.append(claim)
            # Only selected evidence leaves the desk. Full indexed rows are for admission.
            sources[source_id] = {k: source_rows[source_id].get(k) for k in ("id", "path", "content_hash", "prong")}
            # A stray word in a quote or context does not establish that it is
            # an argument of the requested relationship. Only the admitted
            # edge endpoints can ground extra question terms here.
            grounding_text.append(" ".join([*_phrases(a), *_phrases(z)]))
            normalized_topic = " ".join(re.findall(r"[a-z0-9]+", topic.lower()))
            direct_seed.append(bool(predicates is not None and normalized_topic in {
                " ".join(re.findall(r"[a-z0-9]+", p)) for p in _phrases(a)}))
            relation_cues.append(_relation_patterns(topic, row["predicate"], row["predicate_label"]))
        # Do not turn a nearby source into an answer about an additional broker,
        # venue, date, account, jurisdiction or any other unmentioned qualifier.
        # Recognized intent words are covered by the explicit predicate mapping.
        question_for_grounding = topic
        if orb_intent:
            for pattern, predicate in _ORB_INTENTS:
                if predicate == orb_intent:
                    question_for_grounding = re.sub(pattern, " ", question_for_grounding, flags=re.I)
            if orb_intent == "vega_funding_payment":
                question_for_grounding = re.sub(r"\b(?:pay|pays|payer|payment|receiver|receives)\b", " ", question_for_grounding, flags=re.I)
        missing_by_claim = []
        for i, s in enumerate(grounding_text):
            relevant_question = question_for_grounding
            for pattern in relation_cues[i]:
                relevant_question = re.sub(pattern, " ", relevant_question, flags=re.I)
            missing_by_claim.append(sorted(_grounding_words(relevant_question) - _grounding_words(s)))
        missing = min(missing_by_claim, key=len) if missing_by_claim else []
        direct = [bool((orb_intent or direct_seed[i] or relation_cues[i]) and not ungrounded
                       and (not comparison or any(re.search(r"(?:must_exceed|greater_than|less_than)", selected[i]["predicate"], re.I) for _ in relation_cues[i]))
                       and selected[i]["kind"] != "catalog")
                  for i, ungrounded in enumerate(missing_by_claim)]
        answer_status = "unknown" if not selected else ("sourced_paths" if path_mode else
                         ("direct_evidence" if any(direct) else "related_only"))
        for index, claim in enumerate(selected):
            claim["answer_role"] = ("catalog_navigation" if claim["kind"] == "catalog" else "path_hop" if path_mode else
                                    ("direct_evidence" if direct[index] else "related_evidence_only"))
            if not path_mode and not direct[index]:
                rendered[index] = "[Related evidence only] " + rendered[index]
        selected_by_id = {c["id"]: c for c in selected}
        paths = [{"hop_count": len(path), "reached_requested_hops": len(path) == desk_hops,
                  "hops": [selected_by_id[eid] for eid in path]}
                 for path in planned_paths if all(eid in selected_by_id for eid in path)]
        if path_mode and selected and not any(p["reached_requested_hops"] for p in paths):
            answer_status = "incomplete_paths"
        if paths_capped:
            warnings.append("The directed path search was capped at 12 paths.")
        if path_mode and selected:
            summaries = ["Path " + str(i) + ": " + " → ".join(
                [p["hops"][0]["from"]["label"]] + [h["to"]["label"] for h in p["hops"]])
                + (" (no further admitted hop found)" if not p["reached_requested_hops"] else "")
                for i, p in enumerate(paths, 1)]
            rendered.insert(0, "Directed paths: each hop is a separately sourced claim; combining them does not prove causation or performance and contexts may differ."
                            + ("\n" + "\n".join(summaries) if summaries else "\nNo complete path fit the response budget."))
        if not selected:
            rendered = ["No supported, admitted desk evidence found for this question. The answer is unknown from this desk."]
        elif comparison and not any(direct):
            warnings.append("Comparison evidence is sourced separately; the relationship is not asserted unless a cited passage states it.")
            rendered.insert(0, "Comparison evidence is sourced separately; no additional relationship is assumed.")
        if answer_status == "related_only":
            rendered.insert(0, "Related evidence only: the requested detail is not established by these results.")
        text = "> Scope: " + coverage + "\n\n# VEGA desk — " + topic + "\n\n" + "\n\n".join(rendered)
        edges = [{"id": c["id"], "from_id": c["from"]["id"], "to_id": c["to"]["id"],
                  "predicate": c["predicate"], "kind": c["kind"], "status": c["status"],
                  "context": c["context"], "evidence": c["evidence"], "answer_role": c["answer_role"]} for c in selected]
        return DeskPack(topic=topic, nodes=list(sources.values()), claims=selected, edges=edges,
                        answer_status=answer_status, unmatched_terms=missing,
                        paths=paths,
                        text_bundle=text, citations=[s["path"] for s in sources.values()],
                        warnings=warnings, truncated=ranking_pruned or paths_capped or len(selected) < len(claims) or len(paths) < len(planned_paths), used_tokens_est=used)
    finally:
        conn.close()
