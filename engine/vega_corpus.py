"""Bounded, read-only inventory and evidence extraction for the VEGA workspace.

Only authored Markdown in admitted shelves is opened. Paths, including excluded
subtrees, are inventory metadata; paths under excluded directories are never
enumerated. Relationships extracted from prose are candidates for review, not
evidence of present trading authorization or performance.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
import warnings
from pathlib import Path, PurePosixPath

CORPUS_VERSION = 5
_MAX_BYTES = 2_000_000
_TOPIC_KINDS = ["action", "condition", "instrument", "metric", "participant", "position", "process", "risk", "strategy", "corpus_topic"]
_PROTECTED = re.compile(r"(?:memory|founder|credential|secret|journal|account|archive|logs)", re.I)
_PRIVATE_FILE = re.compile(r"(?:^|[ _.-])(?:journal|account|credentials?|secrets?|portfolio|positions?|fills|orders|trades|ledger)(?:[ _.-]|$)", re.I)
_TELEMETRY = re.compile(r"^(?:cycle_|snapshot|telemetry|raw[_-]|.*(?:intraday|premarket|postmarket|daily[_ -]intel))", re.I)
_PRUNE = re.compile(r"^(?:\.git|\.obsidian|\.claude|\.claudian|\.agents|\.pytest_cache|\.planning|\.superpowers|\.dod-requests|node_modules|__pycache__|venv|\.venv|state|logs|sessions?|raw|data|datasets?|cache|backups?|_migration|source-snapshots|automation-outputs|daily intel|jobs|notes|scorecards?)$", re.I)
_TOP = {"01": "overview", "02": "strategies", "03": "execution", "04": "macro", "05": "crypto", "06": "intelligence", "07": "data_sources", "08": "risk", "09": "charts", "77": "back_office", "88": "deliverables", "000": "knowledge"}
_LINK = re.compile(r"\b(?:https?://|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})")
_PRIVATE_LINE = re.compile(r"\b(?:account|credentials?|secrets?|api[ _-]?keys?|passwords?|journal|founder|portfolio|order[ _-]?id|access[ _-]?token)\b", re.I)
_TOPICS = {
    "corpus_donchian": "Donchian breakout", "corpus_trailing_stop": "Trailing stop",
    "corpus_signal_inbox": "Paper signal inbox", "corpus_risk_gate": "Paper risk gate",
    "corpus_reddit_feed": "Reddit RSS feed", "corpus_scanner": "Market scanner",
    "corpus_options_snapshot": "Options snapshot", "corpus_option_chain": "Option chain",
    "corpus_supertrend": "Supertrend indicator", "corpus_atr": "Average true range",
    "corpus_data_reconciliation": "Market data reconciliation", "corpus_data_revisions": "Data revisions",
    "corpus_total_return": "Long-term total return", "corpus_cashflow_growth": "Earnings and cash-flow growth", "corpus_valuation_change": "Valuation change", "corpus_distributions": "Dividend and distribution yield",
    "corpus_structural_stops": "Structural stop placement", "corpus_liquidity_sweep": "Liquidity sweep zones", "corpus_position_reduction": "Reduced position size",
    "corpus_funding_proxy": "Perpetual-versus-spot basis proxy", "corpus_funding_rate": "Crypto funding rate",
    "corpus_cftc_report": "CFTC positioning report", "corpus_release_lag": "Three-day report lag",
    "corpus_drawdown": "Drawdown from a high", "corpus_mispricing": "Mispricing or intact thesis",
    "corpus_discount_rate": "Valuation discount rate", "corpus_terminal_growth": "Terminal growth rate",
}
_TOPIC_DEFINITIONS = {key: label + " (general topic)." for key, label in _TOPICS.items()}



def load_rule_config(path=None):
    """Load explicit caller-owned rules. None means the optional rules are unavailable.

    JSON shape: {"grounded": [{"pattern": "example", "subject": "x", "object": "y"}],
                 "pins": [{"source": "VEGA/example.md", "source_hash": "<64 hex>",
                           "quote": "invented text", "pairs": [["x", "y"]]}]}
    """
    if path is None:
        return None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"grounded", "pins"}:
        raise ValueError("rule configuration requires grounded and pins lists")
    if not all(isinstance(data[k], list) for k in ("grounded", "pins")):
        raise ValueError("rule configuration entries must be lists")
    for entry in data["grounded"]:
        if not isinstance(entry, dict) or set(entry) != {"pattern", "subject", "object"} or not all(isinstance(v, str) and v for v in entry.values()):
            raise ValueError("invalid grounded rule")
        re.compile(entry["pattern"])
    for pin in data["pins"]:
        if not isinstance(pin, dict) or set(pin) != {"source", "source_hash", "quote", "pairs"}:
            raise ValueError("invalid pin")
        source, digest, quote, pairs = (pin[k] for k in ("source", "source_hash", "quote", "pairs"))
        if not isinstance(source, str) or not admitted_markdown_path(source) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or not isinstance(quote, str) or not quote or not isinstance(pairs, list) or not pairs:
            raise ValueError("invalid pin data")
        for pair in pairs:
            if not isinstance(pair, list) or len(pair) != 2 or tuple(pair) not in _PIN_PREDICATES:
                raise ValueError("invalid pin pair")
    return data


def verified_pinned_rules(rule_config=None):
    """Convert caller-provided source-bound pins; no deployment rules are bundled."""
    if rule_config is None:
        return []
    rules = []
    for pin in rule_config["pins"]:
        source, digest, quote, pairs = (pin[k] for k in ("source", "source_hash", "quote", "pairs"))
        for subject, obj in pairs:
            predicate = _PIN_PREDICATES[(subject, obj)]
            rules.append({"source": source, "source_hash": digest, "quote": quote, "from": subject, "to": obj, "predicate": predicate, "kind": "definition", "context": {"scope": "caller-supplied source description"}})
    return rules


_PIN_PREDICATES = {
    ("corpus_total_return", "corpus_cashflow_growth"): "vega_corpus_has_return_component",
    ("corpus_total_return", "corpus_valuation_change"): "vega_corpus_has_return_component",
    ("corpus_total_return", "corpus_distributions"): "vega_corpus_has_return_component",
    ("corpus_structural_stops", "corpus_liquidity_sweep"): "vega_corpus_places_beyond",
    ("corpus_structural_stops", "corpus_position_reduction"): "vega_corpus_pairs_with_smaller_size",
    ("corpus_funding_proxy", "corpus_funding_rate"): "vega_corpus_proxies_for",
    ("corpus_cftc_report", "corpus_release_lag"): "vega_corpus_has_publication_lag",
    ("corpus_signal_inbox", "corpus_risk_gate"): "vega_corpus_routes_through",
    ("corpus_reddit_feed", "corpus_scanner"): "vega_corpus_feeds",
    ("corpus_supertrend", "corpus_atr"): "vega_corpus_uses_indicator",
    ("corpus_options_snapshot", "corpus_option_chain"): "vega_corpus_provides",
    ("corpus_drawdown", "corpus_mispricing"): "vega_corpus_does_not_prove",
    ("corpus_discount_rate", "corpus_terminal_growth"): "vega_corpus_must_exceed",
}


def _id(prefix, value):
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _concept(id_, label, kind, definition):
    return {"id": id_, "label": label, "kind": kind, "area": "vega_corpus", "definition": definition, "aliases": []}


def corpus_concepts():
    return [_concept(key, label, "corpus_topic", _TOPIC_DEFINITIONS[key]) for key, label in _TOPICS.items()]


def corpus_predicates():
    descriptors = [
        {"id": "vega_corpus_located_in", "label": "document is located in", "from_kinds": ["corpus_document"], "to_kinds": ["corpus_shelf"]},
        {"id": "vega_corpus_mentions", "label": "document mentions topic", "from_kinds": ["corpus_document"], "to_kinds": _TOPIC_KINDS},
        {"id": "vega_corpus_requires", "label": "source states requires", "from_kinds": _TOPIC_KINDS, "to_kinds": _TOPIC_KINDS},
        {"id": "vega_corpus_uses", "label": "source states uses", "from_kinds": _TOPIC_KINDS, "to_kinds": _TOPIC_KINDS},
        {"id": "vega_corpus_limits", "label": "source states limits", "from_kinds": _TOPIC_KINDS, "to_kinds": _TOPIC_KINDS},
        {"id": "vega_corpus_measures", "label": "source states measures", "from_kinds": _TOPIC_KINDS, "to_kinds": _TOPIC_KINDS},
        {"id": "vega_corpus_connects", "label": "source explicitly connects", "from_kinds": ["corpus_topic"], "to_kinds": ["corpus_topic"]},
        {"id": "vega_corpus_does_not_prove", "label": "source states does not prove", "from_kinds": ["corpus_topic"], "to_kinds": ["corpus_topic"]},
    ]
    labels = {
        "vega_corpus_has_return_component": "has return component",
        "vega_corpus_places_beyond": "places stop beyond",
        "vega_corpus_pairs_with_smaller_size": "pairs stop placement with smaller position size",
        "vega_corpus_proxies_for": "is a proxy for",
        "vega_corpus_has_publication_lag": "has publication lag",
        "vega_corpus_routes_through": "routes through",
        "vega_corpus_feeds": "feeds",
        "vega_corpus_uses_indicator": "uses indicator input",
        "vega_corpus_provides": "provides",
        "vega_corpus_must_exceed": "must exceed",
    }
    descriptors.extend({"id": key, "label": label, "from_kinds": ["corpus_topic"], "to_kinds": ["corpus_topic"]} for key, label in labels.items())
    return descriptors


def _area(parts):
    if len(parts) < 2:
        return "workspace"
    if parts[1] == "docs":
        return "documentation"
    if parts[1] == "09_Linked_Surfaces":
        return "charts"
    return _TOP.get(parts[1].split(" ", 1)[0], "other")


def _classify(parts, is_dir):
    """Return disposition/reason without opening any content."""
    if len(parts) == 1:
        return None
    name = parts[-1]
    top = parts[1].split(" ", 1)[0]
    if parts[1] == "09_Linked_Surfaces":
        top = "09"
    elif parts[1] == "docs":
        top = "docs"
    if any(_PROTECTED.search(p) for p in parts[1:-1]) or _PROTECTED.search(name):
        return ("excluded", "protected_subtree" if is_dir else "protected_file")
    if any(_PRUNE.match(p) or p.startswith(".") for p in parts[1:]):
        return ("excluded", "pruned_subtree" if is_dir else "pruned_file")
    if (top not in _TOP and top != "docs") or (top == "000" and len(parts) > 2 and parts[2] != "Knowledge"):
        return ("excluded", "outside_authored_shelves")
    if top == "docs" and len(parts) > 2 and parts[2].casefold() != "runbooks":
        return ("excluded", "outside_authored_shelves")
    if top == "77" and len(parts) > 2 and (is_dir or len(parts) > 3) and parts[2].casefold() not in {"runbooks", "systems", "reference", "trading-toolkit", "task records"}:
        return ("excluded", "outside_authored_shelves")
    if top == "77" and len(parts) > 3 and parts[2].casefold() == "task records" and not is_dir and not re.search(r"(?:strategy|playbook|research)", name, re.I):
        return ("excluded", "work_log_or_security_review")
    if _PRIVATE_FILE.search(name):
        return ("excluded", "protected_subtree" if is_dir else "protected_file")
    if not is_dir and _TELEMETRY.match(name):
        return ("excluded", "machine_telemetry")
    if not is_dir and Path(name).suffix.casefold() != ".md":
        suffix = Path(name).suffix.casefold()
        reason = ("raw_data_format" if suffix in {".csv", ".tsv", ".parquet", ".sqlite", ".db"}
                  else "code_format" if suffix in {".py", ".js", ".ts", ".pine", ".sh", ".sql"}
                  else "document_format" if suffix in {".pdf", ".html", ".htm", ".docx"}
                  else "machine_format" if suffix in {".json", ".jsonl", ".yaml", ".yml"}
                  else "unsupported_format")
        return ("unsupported", reason)
    return None


def admitted_markdown_path(relative):
    """Classify a normalized Desktop-relative VEGA path without filesystem IO.

    Callers must independently refuse symlinks and verify the source hash at
    read time; a lexical admission cannot establish a file's current identity.
    """
    if not isinstance(relative, str) or "\\" in relative or "\x00" in relative:
        return False
    path = PurePosixPath(relative)
    if path.is_absolute() or path.as_posix() != relative or len(path.parts) < 3 or path.parts[0] != "VEGA" or any(part in {".", ".."} for part in relative.split("/")):
        return False
    return all(_classify(path.parts[:i], True) is None for i in range(2, len(path.parts))) and _classify(path.parts, False) is None


def excluded_directory(relative):
    """True when a directory at this VEGA path (or an ancestor) is lexically excluded.

    Such a directory is a terminal inventory record: scans never enumerate it.
    """
    parts = PurePosixPath(relative).parts
    if len(parts) < 2 or parts[0] != "VEGA":
        return False
    return any(_classify(parts[:end], True) for end in range(2, len(parts) + 1))


def _record(source, disposition, reason, **kwargs):
    return {"source": source, "area": _area(PurePosixPath(source).parts), "disposition": disposition, "reason": reason, "source_hash": None, "concepts": [], "rules": [], **kwargs}


def _vocab(pack):
    # Reject duplicate aliases; a short acronym must be explicitly uppercase.
    names = {}
    kinds = set()
    for c in pack.get("concepts", []):
        kinds.add(c["kind"])
        for name in [c["label"], *c.get("aliases", [])]:
            if name.casefold() in {"vega", "base"} or (len(name) < 4 and not (len(name) >= 3 and name.isupper())):
                continue
            if "\n" in name or len(name) > 80:
                continue
            names.setdefault(name.casefold(), set()).add(c["id"])
    names = {k: next(iter(v)) for k, v in names.items() if len(v) == 1}
    if not names:
        return None, {}, kinds
    pattern = re.compile(r"(?<![\w])(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + r")(?![\w])", re.I)
    return pattern, names, kinds


def _body_lines(text):
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        for i in range(1, min(len(lines), 200)):
            if lines[i].strip() == "---":
                lines = lines[i + 1:]
                break
    fenced = False
    for raw in lines:
        line = raw.strip()
        if line.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or not line or len(line) > 600 or _LINK.search(line) or _PRIVATE_LINE.search(line):
            continue
        yield line


_RELATION = re.compile(r"^\s*(?P<verb>requires?|needs?|uses?|employs?|limits?|caps?|measures?)\s+(?:an?\s+|the\s+)?$", re.I)
_VERBS = {"require": "requires", "need": "requires", "use": "uses", "employ": "uses", "limit": "limits", "cap": "limits", "measure": "measures"}


def _extract(source, body, pack, pattern, names, grounded=()):
    doc = _id("doc_", source)
    parent = str(PurePosixPath(source).parent)
    shelf = _id("shelf_", parent)
    concepts = [_concept(doc, PurePosixPath(source).name, "corpus_document", "Authored VEGA source document."), _concept(shelf, PurePosixPath(parent).name, "corpus_shelf", "VEGA workspace source location.")]
    rules = []
    mentioned = set()
    seen_rel = set()
    for line in _body_lines(body):
        if not rules:
            rules.append({"source": source, "quote": line, "from": doc, "predicate": "vega_corpus_located_in", "to": shelf, "kind": "catalog"})
        for regex, subject, obj in grounded:
            key = (subject, "connects", obj, line)
            if regex.search(line) and key not in seen_rel and not re.search(r"\b(?:not|never|without|may|might|could|would|if|unless|whether)\b|\?", line, re.I):
                rules.append({"source": source, "quote": line, "from": subject, "predicate": "vega_corpus_connects", "to": obj, "kind": "hypothesis", "inferred": True, "context": {"extraction": "explicit source wording; review historical scope"}})
                seen_rel.add(key)
        if not pattern:
            continue
        hits = [(m.start(), m.end(), names[m.group().casefold()]) for m in pattern.finditer(line)]
        for _, _, topic in hits:
            if topic not in mentioned and len(mentioned) < 40:
                rules.append({"source": source, "quote": line, "from": doc, "predicate": "vega_corpus_mentions", "to": topic, "kind": "catalog"})
                mentioned.add(topic)
        # Simple two-term subject/verb/object matches only; tables, negation,
        # questions and hypothesized statements never become asserted links.
        if line.startswith(("|", "#", ">")) or re.search(r"\b(?:not|never|without|may|might|could|would|if|unless|whether)\b|\?", line, re.I):
            continue
        for left, right in zip(hits, hits[1:]):
            match = _RELATION.fullmatch(line[left[1]:right[0]])
            if not match or left[2] == right[2]:
                continue
            verb = match.group("verb").casefold().rstrip("s")
            predicate = _VERBS.get(verb)
            key = (left[2], predicate, right[2], line)
            if predicate and key not in seen_rel and len(seen_rel) < 40:
                rules.append({"source": source, "quote": line, "from": left[2], "predicate": "vega_corpus_" + predicate, "to": right[2], "kind": "hypothesis", "inferred": True, "context": {"extraction": "candidate relationship in historical source; review original wording"}})
                seen_rel.add(key)
    return concepts, rules


# Per-process cache of eligible-file results. A full reconcile re-walks every
# path, but re-reading and re-extracting ~2k unchanged documents cost ~30 s of
# CPU per watcher flush. An entry is reused only when the file's identity and
# change stamps (device, inode, size, mtime_ns, ctime_ns) and the extraction
# context (pack, corpus version, pins) are all unchanged. Files changed within
# _RACY_NS of being read are never cached, so a same-tick rewrite is re-read.
_RECORD_CACHE = {}
_RECORD_CACHE_CONTEXT = [None]
_RACY_NS = 2_000_000_000


def _stat_key(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _cache_context(pack, pins_by_source):
    blob = json.dumps([CORPUS_VERSION, pack, pins_by_source], sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def clear_record_cache():
    _RECORD_CACHE.clear()
    _RECORD_CACHE_CONTEXT[0] = None


def scan(root, *, paths=None, pack, rule_config=None):
    """Yield one record per traversed file or pruned subtree, without writes.

    ``root`` is Desktop (or its VEGA child). ``paths`` accepts VEGA-relative or
    Desktop-relative names; missing selections yield removed records. Symlinks
    are never followed. Duplicate hashes are reported within the current pass;
    each file retains its own evidence, allowing independent incremental updates.
    """
    if rule_config is None:
        warnings.warn("Optional VEGA source rules are unavailable: no caller configuration supplied", RuntimeWarning, stacklevel=2)
    root = Path(root).absolute()
    base = root if root.name == "VEGA" else root / "VEGA"
    pattern, names, _ = _vocab(pack)
    seen_hash, visited = {}, set()
    pins_by_source = {}
    grounded = [(re.compile(x["pattern"], re.I), x["subject"], x["object"]) for x in (rule_config or {}).get("grounded", [])]
    for pin in verified_pinned_rules(rule_config):
        pins_by_source.setdefault(pin["source"], []).append(pin)
    context = _cache_context([pack, rule_config], pins_by_source)
    if _RECORD_CACHE_CONTEXT[0] != context:
        clear_record_cache()
        _RECORD_CACHE_CONTEXT[0] = context
    base_key = str(base)

    def visit(path):
        rel = PurePosixPath("VEGA", *path.relative_to(base).parts).as_posix()
        if rel in visited:
            return
        visited.add(rel)
        try:
            if path.is_symlink():
                yield _record(rel, "excluded", "symlink")
                return
            if not path.exists():
                yield _record(rel, "removed", "missing")
                return
            is_dir = path.is_dir()
            classification = _classify(PurePosixPath(rel).parts, is_dir)
            if classification:
                yield _record(rel, *classification)
                return
            if is_dir:
                try:
                    entries = sorted(path.iterdir(), key=lambda p: p.name.casefold())
                except OSError:
                    yield _record(rel, "read_error", "directory_unreadable")
                    return
                for child in entries:
                    yield from visit(child)
                return
            if not path.is_file():
                yield _record(rel, "unsupported", "special_file")
                return
            st = path.stat()
            if st.st_size > _MAX_BYTES:
                yield _record(rel, "unsupported", "file_too_large")
                return
            cache_key = (base_key, rel)
            cached = _RECORD_CACHE.get(cache_key)
            if cached is not None and cached[0] == _stat_key(st):
                _, digest, concepts, rules = cached
                concepts, rules = copy.deepcopy(concepts), copy.deepcopy(rules)
            else:
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(path, flags)
                with os.fdopen(fd, "rb") as stream:
                    # Stamp taken before the read: a write racing the read
                    # moves mtime/ctime past this key, so it can never hit.
                    opened = os.fstat(stream.fileno())
                    raw = stream.read(_MAX_BYTES + 1)
                _RECORD_CACHE.pop(cache_key, None)
                if len(raw) > _MAX_BYTES:
                    yield _record(rel, "unsupported", "file_too_large")
                    return
                try:
                    body = raw.decode("utf-8")
                except UnicodeDecodeError:
                    yield _record(rel, "unsupported", "invalid_utf8")
                    return
                digest = hashlib.sha256(raw).hexdigest()
                concepts, rules = _extract(rel, body, pack, pattern, names, grounded)
                pins = [p for p in pins_by_source.get(rel, []) if p["source_hash"] == digest and p["quote"] in body]
                if pins and not any(r["predicate"] == "vega_corpus_located_in" for r in rules):
                    rules.append({"source": rel, "quote": pins[0]["quote"], "from": _id("doc_", rel), "predicate": "vega_corpus_located_in", "to": _id("shelf_", str(PurePosixPath(rel).parent)), "kind": "catalog"})
                for pin in pins:
                    rules = [r for r in rules if not (r.get("inferred") and all(r.get(k) == pin[k] for k in ("source", "quote", "from", "to")))]
                    rules.append({k: v for k, v in pin.items() if k != "source_hash"})
                key = _stat_key(opened)
                if key == _stat_key(st) and time.time_ns() - max(opened.st_mtime_ns, opened.st_ctime_ns) > _RACY_NS:
                    _RECORD_CACHE[cache_key] = (key, digest, copy.deepcopy(concepts), copy.deepcopy(rules))
            prior = seen_hash.setdefault(digest, rel)
            yield _record(rel, "eligible", "authored_markdown", source_hash=digest, concepts=concepts, rules=rules, duplicate_of=prior if prior != rel else None)
        except OSError:
            yield _record(rel, "read_error", "source_unreadable")

    selected = [base] if paths is None else []
    if paths is not None:
        for value in paths:
            pure = PurePosixPath(str(value))
            if pure.is_absolute() or ".." in pure.parts or "\\" in str(value):
                raise ValueError("paths must be normalized VEGA-relative paths")
            parts = pure.parts[1:] if pure.parts and pure.parts[0] == "VEGA" else pure.parts
            target = base.joinpath(*parts)
            # Check each ancestor without following a symlink, even for explicit selections.
            cursor = base
            blocked = False
            for part in parts[:-1]:
                cursor = cursor / part
                if cursor.is_symlink():
                    yield _record(PurePosixPath("VEGA", *parts).as_posix(), "excluded", "symlink")
                    blocked = True
                    break
                cls = _classify(PurePosixPath("VEGA", *cursor.relative_to(base).parts).parts, True)
                if cls:
                    yield _record(PurePosixPath("VEGA", *parts).as_posix(), *cls)
                    blocked = True
                    break
            if not blocked:
                selected.append(target)
    for target in selected:
        yield from visit(target)
    if paths is None:
        # A completed full walk knows every live path; drop entries for the rest.
        for stale in [k for k in _RECORD_CACHE if k[0] == base_key and k[1] not in visited]:
            del _RECORD_CACHE[stale]
