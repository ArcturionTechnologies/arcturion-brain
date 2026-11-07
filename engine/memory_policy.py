"""Process-scoped pilot: host policy, never note/request identity, controls access.

Same-OS-user/launcher/code is trusted. This is not multi-tenant authentication
or a production gateway action gate. No network, writes, or policy caching.
"""
from __future__ import annotations
import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

class PolicyError(RuntimeError):
    pass

def _dependencies(value):
    if not isinstance(value, list) or len(value) > 128:
        raise PolicyError("invalid dependency list")
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] or not isinstance(item.get("content_hash"), str) or not item["content_hash"]:
            raise PolicyError("invalid version dependency")
    return value

def _metadata(node):
    value = node.get("metadata") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise PolicyError("invalid node metadata") from exc
    if not isinstance(value, dict):
        raise PolicyError("invalid node metadata")
    return value

def _stamp(value):
    if not isinstance(value, str):
        raise ValueError("missing validity")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("validity requires timezone")
    return result

class Policy:
    def __init__(self, data=None):
        self.data = data
        self.records = data["records"] if data else {}
        self.identity = {"principal": data["principal"], "capacity": data["capacity"]} if data else None

    def allows(self, node, resolve):
        """Validate exact indexed version, ACL, and transitive declared dependencies."""
        visiting = set()
        completed = {}
        def check(current, depth=0):
            if current is None or depth > 128:
                return False
            key = (current.get("id"), current.get("content_hash") or "")
            if not isinstance(key[0], str) or not key[0] or not isinstance(key[1], str):
                return False
            if key in visiting:
                return False
            if key in completed:
                return completed[key]
            visiting.add(key)
            try:
                record = self.records.get(key[0])
                md = _metadata(current)
                if record is not None:
                    if record["content_hash"] != key[1] or record.get("withdrawn", False):
                        return False
                    if record.get("path") is not None and record["path"] != current.get("path"):
                        return False
                    if "readers" in record and self.identity not in record["readers"]:
                        return False
                # Metadata may only narrow host policy; never authenticate/grant.
                if md.get("memory_restricted") and record is None:
                    return False
                if "memory_readers" in md and (not isinstance(md["memory_readers"], list) or self.identity is None or self.identity not in md["memory_readers"]):
                    return False
                deps = _dependencies(record.get("dependencies", [])) if record else []
                deps = deps + _dependencies(md.get("memory_dependencies", []))
                for dependency in deps:
                    parent = resolve(dependency["id"])
                    if parent is None or parent.get("content_hash") != dependency["content_hash"] or not check(parent, depth+1):
                        return False
                completed[key] = True
                return True
            except (PolicyError, TypeError, KeyError):
                return False
            finally:
                visiting.discard(key)
        return check(node)

    def action_eligibility(self, request, resolve, now=None):
        """Current eligibility only. Historical recall can never resurrect a grant."""
        deny = lambda reason: {"allowed": False, "reason": reason}
        if not self.data:
            return deny("no_trusted_policy")
        if not isinstance(request, dict):
            return deny("invalid_request")
        grants = [g for g in self.data["grants"] if g.get("id") == request.get("grant_id")]
        if len(grants) != 1:
            return deny("grant_absent_or_ambiguous")
        grant = grants[0]
        if grant.get("issuer") not in self.data["issuers"]:
            return deny("unknown_issuer")
        if any(grant.get(k) != self.identity[k] for k in self.identity):
            return deny("principal_capacity_mismatch")
        if grant.get("revoked") is not False:
            return deny("revoked_or_unknown")
        if type(grant.get("version")) is not int or type(request.get("grant_version")) is not int or grant["version"] != request["grant_version"]:
            return deny("grant_version_changed")
        if not all(isinstance(request.get(k), str) and request[k] and request[k] == grant.get(k) for k in ("action", "scope")):
            return deny("action_scope_mismatch")
        try:
            current = now or datetime.now(timezone.utc)
            if not _stamp(grant["valid_from"]) <= current < _stamp(grant["valid_until"]):
                return deny("outside_validity")
            dependencies = _dependencies(grant["dependencies"])
            for dependency in dependencies:
                node = resolve(dependency["id"])
                if node is None or node.get("content_hash") != dependency["content_hash"] or not self.allows(node, resolve):
                    return deny("dependency_unavailable")
        except (KeyError, ValueError, TypeError, PolicyError):
            return deny("invalid_grant")
        return {"allowed": True, "grant_id": grant["id"], "grant_version": grant["version"],
                "principal": self.identity["principal"], "capacity": self.identity["capacity"],
                "action": grant["action"], "scope": grant["scope"]}

    def file_resolver(self, node_id):
        """Trusted typed dependency resolver; Brain sources retain current quarantine.

        File records have no indexed metadata; their restrictions are in host policy.
        """
        record = self.records.get(node_id)
        if record and record.get("kind") == "brain":
            import sqlite3
            from contextlib import closing
            from . import config as C
            from .source_integrity import filter_retrieval
            try:
                with closing(sqlite3.connect(C.BRAIN_DB.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute('SELECT * FROM nodes WHERE id=?', (node_id,)).fetchone()
                    allowed = filter_retrieval(conn, [dict(row)] if row else [], historical=False)
                    return allowed[0] if allowed else None
            except (OSError, sqlite3.Error, RuntimeError):
                return None
        if not record or not isinstance(record.get("path"), str):
            return None
        path = Path(record["path"])
        if not path.is_absolute():
            return None
        try:
            raw = path.read_bytes()
        except OSError:
            return None
        return {"id": node_id, "content_hash": hashlib.sha256(raw).hexdigest(), "path": str(path)}

def load_policy():
    """Policy and expected identity are trusted launcher environment, never tool args."""
    values = [os.environ.get(k) for k in ("ARCTURION_MEMORY_POLICY", "ARCTURION_MEMORY_PRINCIPAL", "ARCTURION_MEMORY_CAPACITY")]
    if not any(values):
        return Policy()
    if not all(values):
        raise PolicyError("incomplete trusted memory policy configuration")
    path, principal, capacity = values
    candidate = Path(path)
    if not candidate.is_absolute():
        raise PolicyError("policy path must be absolute")
    try:
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022 or info.st_size > 1024*1024:
                raise PolicyError("unsafe policy ownership, permissions, type, or size")
            data = json.loads(stream.read(1024*1024+1))
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1 or data.get("principal") != principal or data.get("capacity") != capacity or not principal.strip() or capacity not in {"business", "personal"}:
            raise PolicyError("policy schema or launcher identity mismatch")
        if not isinstance(data.get("records"), dict) or not isinstance(data.get("grants"), list) or not isinstance(data.get("issuers"), list):
            raise PolicyError("invalid policy collections")
        if not all(isinstance(x, str) and x for x in data["issuers"]):
            raise PolicyError("invalid issuer registry")
        if not all(isinstance(g, dict) for g in data["grants"]):
            raise PolicyError("invalid grants")
        if any(not isinstance(g.get('id'), str) or not g['id'].strip() for g in data['grants']):
            raise PolicyError("invalid grant identity")
        for node_id, record in data["records"].items():
            if not isinstance(node_id, str) or not node_id or not isinstance(record, dict) or not isinstance(record.get("content_hash"), str) or not record["content_hash"]:
                raise PolicyError("invalid registered version")
            if "withdrawn" in record and type(record["withdrawn"]) is not bool:
                raise PolicyError("invalid withdrawal")
            if record.get('kind', 'file') not in {'file', 'brain'}:
                raise PolicyError("invalid dependency source kind")
            if "readers" in record and (not isinstance(record["readers"], list) or not all(isinstance(x, dict) and set(x) == {"principal", "capacity"} and all(isinstance(v, str) and v for v in x.values()) for x in record["readers"])):
                raise PolicyError("invalid reader policy")
            _dependencies(record.get("dependencies", []))
        return Policy(data)
    except (OSError, ValueError, TypeError) as exc:
        raise PolicyError("configured memory policy unavailable or invalid") from exc

def guarded_call(policy, request, resolve, callback):
    """Internal pilot seam; observing this callback is not gateway enforcement proof."""
    eligibility = policy.action_eligibility(request, resolve)
    if not eligibility["allowed"]:
        raise PermissionError(eligibility["reason"])
    return callback()
