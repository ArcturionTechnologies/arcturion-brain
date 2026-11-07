"""Conservative file-level Python dependencies with qualified symbol provenance.

No execution, imports, sys.path search or bare-method matching. Unknown dynamic
receivers, nested scopes and rebinding are explicitly unresolved. Static edges
describe source declarations, never proof that a runtime call occurred.
"""
import ast
import hashlib
import json
from pathlib import PurePosixPath
from .util import now_iso
VERSION = 1
ASSERTION = "STATIC_AST_QUALIFIED"

def _loads(value):
    try:
        return json.loads(value) if isinstance(value, str) else (value or {})
    except (TypeError, ValueError):
        return {}

def analyze(text, path):
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return {"version": VERSION, "parse_error": True, "symbols": [], "references": []}
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    def scope(n):
        parts = []
        while n in parents:
            n = parents[n]
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                parts.append(n.name)
        return ".".join(reversed(parts))
    # Only direct, undecorated module functions/classes and direct class methods
    # form verified symbol declarations. Rebound declarations are never resolved.
    bindings = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bindings.setdefault(scope(n), set()).add(n.id)
        if isinstance(n, ast.arg):
            bindings.setdefault(scope(n), set()).add(n.arg)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bindings.setdefault(scope(n), set()).add(n.name)
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                bindings.setdefault(scope(n), set()).add(a.asname or (a.name.split(".")[0] if isinstance(n, ast.Import) else a.name))
        # Python also stores bindings as strings rather than Name(Store).
        extra = []
        if isinstance(n, ast.ExceptHandler) and n.name: extra = [n.name]
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name: extra = [n.name]
        elif isinstance(n, ast.MatchMapping) and n.rest: extra = [n.rest]
        elif isinstance(n, (ast.Global, ast.Nonlocal)): extra = n.names
        if extra: bindings.setdefault(scope(n), set()).update(extra)
    symbols = []
    safe = {}
    imports = {}
    counts = {}
    star_import = False
    for n in ast.walk(tree):
        if scope(n):
            continue
        names = []
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names = [n.id]
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names = [n.name]
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names = [a.asname or (a.name.split(".")[0] if isinstance(n, ast.Import) else a.name) for a in n.names]
            star_import |= any(a.name == "*" for a in n.names)
        elif isinstance(n, ast.ExceptHandler) and n.name: names = [n.name]
        elif isinstance(n, (ast.MatchAs, ast.MatchStar)) and n.name: names = [n.name]
        elif isinstance(n, ast.MatchMapping) and n.rest: names = [n.rest]
        if isinstance(n, ast.Attribute) and isinstance(n.ctx,(ast.Store,ast.Del)):
            base = n.value
            while isinstance(base, ast.Attribute): base = base.value
            if isinstance(base,ast.Name): names.append(base.id)
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not n.decorator_list:
                safe[n.name] = n
                symbols.append(n.name)
                if isinstance(n, ast.ClassDef) and not n.bases and not n.keywords:
                    method_names = {}
                    declared = set()
                    for m in ast.walk(n):
                        if scope(m) != n.name:
                            continue
                        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            declared.add(m.name)
                            method_names[m.name] = method_names.get(m.name, 0) + (2 if m.decorator_list else 1)
                        else:
                            names = []
                            if isinstance(m, ast.Name) and isinstance(m.ctx, (ast.Store, ast.Del)): names = [m.id]
                            elif isinstance(m, ast.ClassDef): names = [m.name]
                            elif isinstance(m, (ast.Import, ast.ImportFrom)):
                                names = [a.asname or a.name.split(".")[0] for a in m.names]
                            elif isinstance(m, ast.ExceptHandler) and m.name: names = [m.name]
                            elif isinstance(m, (ast.MatchAs, ast.MatchStar)) and m.name: names = [m.name]
                            elif isinstance(m, ast.MatchMapping) and m.rest: names = [m.rest]
                            for name in names: method_names[name] = method_names.get(name, 0) + 2
                    symbols += [n.name+"."+name for name in sorted(declared) if method_names.get(name) == 1]
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                bound = a.asname or (a.name.split(".")[0] if isinstance(n, ast.Import) else a.name)
                if a.name != "*":
                    imports[bound] = {"module": a.name if isinstance(n, ast.Import) else (n.module or ""),
                                      "level": 0 if isinstance(n, ast.Import) else n.level,
                                      "symbol": "" if isinstance(n, ast.Import) else a.name,
                                      "module_alias": isinstance(n, ast.Import),
                                      "bound": bound,
                                      "unaliased_dotted": isinstance(n, ast.Import) and not a.asname and "." in a.name}
    # A global declaration can redirect a later assignment to the module.
    # Conservatively invalidate even read-only declarations rather than guess.
    for declaration in ast.walk(tree):
        if isinstance(declaration, ast.Global):
            for name in declaration.names: counts[name] = 2
    symbols = [s for s in symbols if counts.get(s.split(".")[0]) == 1]
    if star_import: symbols = []
    refs = []
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                refs.append({"kind": "imports", "line": n.lineno, "expression": ast.unparse(n),
                             "caller": scope(n) or "<module>",
                             "module": a.name if isinstance(n, ast.Import) else (n.module or ""),
                             "level": 0 if isinstance(n, ast.Import) else n.level,
                             "symbol": "", "reason": "module_reference"})
        if not isinstance(n, ast.Call):
            continue
        def dotted(f):
            if isinstance(f, ast.Name):
                return f.id
            if isinstance(f, ast.Attribute):
                parent = dotted(f.value)
                return parent + "." + f.attr if parent else None
            return None
        expr = dotted(n.func) or "<dynamic:" + type(n.func).__name__ + ">"
        caller = scope(n) or "<module>"
        ref = {"kind": "calls", "line": n.lineno, "expression": expr, "caller": caller,
               "reason": "dynamic_or_external"}
        chain = expr.split(".")
        base = chain[0]
        # Only one function lexical scope; lambdas/comprehensions/nested functions
        # are deliberately conservative because their bindings differ.
        ancestors = []
        x = n
        while x in parents:
            x = parents[x]; ancestors.append(x)
        nested = sum(isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) for x in ancestors)>1
        dynamic_scope = nested or any(isinstance(x,(ast.Lambda,ast.comprehension,ast.ListComp,ast.SetComp,ast.DictComp,ast.GeneratorExp)) for x in ancestors)
        local_shadow = any(base in bindings.get(scope(x)+"."+x.name if scope(x) else x.name,set())
                           for x in ancestors if isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)))
        class_body = any(isinstance(x,ast.ClassDef) for x in ancestors) and not any(isinstance(x,(ast.FunctionDef,ast.AsyncFunctionDef)) for x in ancestors)
        if dynamic_scope or local_shadow or star_import or class_body:
            ref["reason"] = "shadowed_or_nested"
        elif all(s.isidentifier() for s in chain):
            if expr in symbols and counts.get(base) == 1:
                ref.update(local_symbol=expr, reason="local_declaration")
            elif base in imports and counts.get(base) == 1:
                imp = dict(imports[base])
                suffix = ".".join(chain[1:])
                if imp.pop("unaliased_dotted") and not expr.startswith(imp["module"]+"."):
                    pass
                else:
                    if "." in imp["module"] and not imports[base].get("unaliased_dotted"):
                        pass
                    elif imports[base].get("unaliased_dotted"):
                        suffix = expr[len(imp["module"])+1:]
                    imp["symbol"] = ".".join(s for s in (imp["symbol"],suffix) if s)
                    ref.update(imp,reason="imported_declaration")
        refs.append(ref)
    return {"version": VERSION, "symbols": symbols, "references": refs,
            "reference_count": len(refs), "truncated": False}

def _module_candidates(path, module, level, indexed_paths):
    p = PurePosixPath(path)
    if level:
        base = p.parent
        for _ in range(level-1):
            if str(base) in (".","/") or str(base/"__init__.py") not in indexed_paths: return []
            base = base.parent
        if str(base/"__init__.py") not in indexed_paths: return []
        # Relative imports require an indexed package, not a guessed parent.
        if str(p.parent/"__init__.py") not in indexed_paths: return []
        bases = [base]
    else:
        package_root = p.parent
        while str(package_root/"__init__.py") in indexed_paths:
            package_root = package_root.parent
        bases = [package_root]
    suffix = PurePosixPath(*module.split(".")) if module else PurePosixPath(".")
    paths = set()
    for base in bases:
        target = base/suffix
        candidates = [str(target.with_suffix(".py")),str(target/"__init__.py")] if module else [str(target/"__init__.py")]
        paths.update(x for x in candidates if x in indexed_paths)
    return sorted(paths)

def resolved_edges(node, catalog):
    meta = _loads(node.get("metadata"))
    analysis = meta.get("python_analysis", {})
    refs = analysis.get("references", [])
    path = node["path"]
    groups = {}
    for ref in refs:
        target = None
        symbol = ref.get("local_symbol")
        reason = ref["reason"]
        qualified = None
        target_hash = None
        if symbol:
            target = node["id"]; qualified = path+"::"+symbol; target_hash=node.get("content_hash")
        elif "module" in ref:
            matches = _module_candidates(path,ref["module"],ref.get("level",0),catalog)
            targets = [n for p in matches for n in catalog[p]]
            if len(targets) == 1:
                other = targets[0]
                declared = _loads(other.get("metadata")).get("python_analysis",{}).get("symbols",[])
                imported = ref.get("symbol","")
                if ref["kind"] == "imports" or imported in declared:
                    target = other["id"]
                    target_hash = other.get("content_hash")
                    qualified = other["path"]+"::"+(imported or "<module>")
                    reason = "verified_module_symbol" if imported else "verified_module"
                else: reason = "unverified_imported_symbol"
            else:
                reason = "ambiguous_module" if len(targets)>1 else "external_or_unindexed_module"
        callsite = {**ref, "caller_qualified": path+"::"+ref["caller"],
                    "callee_qualified": qualified, "target_content_hash": target_hash, "resolution": "resolved" if target else "unresolved",
                    "resolution_reason": reason}
        if not target:
            identity = json.dumps([path,ref["caller"],ref["expression"],ref["kind"]],ensure_ascii=False)
            target = "unresolved_code_"+hashlib.sha256(identity.encode()).hexdigest()[:24]
        key = (ref["kind"],target)
        groups.setdefault(key,[]).append(callsite)
    edges = []
    for (predicate,target),sites in groups.items():
        resolved = sites[0]["resolution"]=="resolved"
        edges.append({"from":node["id"],"to":target,"predicate":predicate,"family":"code",
                      "asserted_by":ASSERTION,"asserted_at":now_iso(),"weight":1.0 if resolved else 0.0,
                      "confidence":0.95 if resolved else 0.0,
                      "evidence_span":"python-qualified-static-v1",
                      "metadata":{"resolution":"resolved" if resolved else "unresolved",
                                  "graph_level":"file","callsites":sites,"callsite_count":len(sites),
                                  "truncated":False,"source_hash":node.get("content_hash")}})
    return edges

def refresh(conn, node_ids=None):
    from . import db as DB
    rows = [dict(r) for r in conn.execute("SELECT id,path,metadata,content_hash FROM nodes WHERE path LIKE '%.py'")]
    catalog = {}
    selected = []
    for n in rows:
        if not allowed_source(n["path"]): continue
        catalog.setdefault(n["path"],[]).append(n)
        if _loads(n["metadata"]).get("python_analysis",{}).get("version")==VERSION:
            if node_ids is None or n["id"] in node_ids:
                selected.append(n)
    result = {"files":len(selected),"resolved":0,"unresolved":0,"cross_file":0,"self":0}
    for n in selected:
        old = [dict(r) for r in conn.execute("SELECT * FROM edges WHERE (from_id=? AND predicate IN ('calls','imports')) OR (to_id=? AND predicate IN ('called_by','imported_by'))", (n["id"],n["id"]))]
        for edge in owned_edges(old, {n["id"]}):
            conn.execute("DELETE FROM edges WHERE id=?", (edge["id"],))
        for edge in resolved_edges(n,catalog):
            DB.insert_edge(conn,edge)
            state=edge["metadata"]["resolution"];result[state]+=1
            if state=="resolved": result["self" if edge["to"]==edge["from"] else "cross_file"]+=1
    return result


def allowed_source(path):
    from pathlib import Path
    from . import config as C
    from .source_integrity import source_path
    from .util import should_skip_path
    if not path or "ATLAS" in PurePosixPath(path).parts: return False
    p = source_path(path)
    if p is None: return False
    try:
        rel = p.resolve().relative_to(C.DESKTOP_ROOT.resolve())
        allowed = {C.UNIVERSE_ROOT.name,C.BRAIN_ROOT.name,C.RUNTIME_ROOT.name,*C.CONSTELLATION_AGENTS}
        return rel.parts[0] in allowed and "ATLAS" not in rel.parts and not should_skip_path(p)
    except (ValueError,OSError):
        return False

def owned_edges(rows, ids):
    direct = [e for e in rows if e["from_id"] in ids and e["predicate"] in ("calls","imports")
              and e["asserted_by"] in ("STATIC_AST",ASSERTION)]
    direct_ids = {e["id"] for e in direct}
    pairs = {(e["from_id"],e["to_id"],e["predicate"]) for e in direct}
    inverse = {"called_by":"calls","imported_by":"imports"}
    return [e for e in rows if e["id"] in direct_ids or
            (e["asserted_by"]=="INVERSE_DERIVATION" and
             ((e["from_id"],e["to_id"],e["predicate"]) in pairs or
              (e["to_id"],e["from_id"],inverse.get(e["predicate"])) in pairs))]
