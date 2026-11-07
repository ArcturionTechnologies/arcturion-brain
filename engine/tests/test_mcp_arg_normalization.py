"""Regression: MCP tool arguments must never be discarded in silence.

Root cause guarded (2026-07-31): `brain_propose_node` declares the markdown
field as `body`, but callers naturally pass `content` — the very name
`brain_validate_write` uses for the same idea. The dispatcher read `args.get("body")`,
found nothing, and staged the proposal anyway, reporting success. The entire
decision body was gone. **Seven of the nine proposals staged between 2026-07-16
and 2026-07-29 lost their bodies this way**, including Law L015 and a Law L011
companion — Tier-0 doctrine reduced to a one-line summary.

The same class of defect made missing required fields unreadable: a bare
`args["family"]` reached the caller as `Tool brain_propose_node failed: 'family'`,
with no statement of the contract, so callers rediscovered the schema one
KeyError at a time. `brain_search` was outright unusable from a natural call —
`failed: 'text'` when passed `query`.

Guards:
  1. `content` is captured as `body` — the exact bug. A staged proposal must
     carry its markdown.
  2. An alias NEVER clobbers a value the caller supplied under the canonical
     name; the loser is reported, not silently dropped.
  3. An argument matching nothing is reported back on the result with a warning
     — never swallowed.
  4. Missing required fields return a readable contract (missing + required +
     accepted), not a KeyError.
  5. `brain_search` accepts `query`/`topic` for `text`.
  6. Every tool in REQUIRED_ARGS declares required fields consistent with its
     own published inputSchema — the two must not drift apart.

Run:  python3 -m engine.tests.test_mcp_arg_normalization
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from .. import mcp_server as M


def _call(name: str, args: dict):
    resp = M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": name, "arguments": args}})
    return json.loads(resp["result"]["content"][0]["text"])


def _staged_record(staged_path: str) -> dict:
    path = M.STAGING_DIR / Path(staged_path).name
    return json.loads(path.read_text().splitlines()[0])


def run() -> int:
    orig_staging = M.STAGING_DIR
    tmp = tempfile.TemporaryDirectory()
    M.STAGING_DIR = Path(tmp.name) / "proposals"
    M.STAGING_DIR.mkdir(parents=True, exist_ok=True)
    base = {"node_type": "fact", "family": "concept", "asserted_by": "TEST"}
    try:
        # --- Guard 1: `content` survives as `body` (the 2026-07-31 defect) ---
        out = _call("brain_propose_node",
                    {**base, "title": "argnorm g1", "summary": "s",
                     "content": "THIS BODY MUST SURVIVE"})
        assert "error" not in out, out
        rec = _staged_record(out["staged_path"])
        assert rec["body"] == "THIS BODY MUST SURVIVE", \
            f"content alias dropped — body was {rec['body']!r}"
        assert "content → body" in out.get("aliased_arguments", []), \
            "alias applied but not disclosed to the caller"

        # --- Guard 2: an alias must not overwrite an explicit canonical value ---
        out = _call("brain_propose_node",
                    {**base, "title": "argnorm g2", "summary": "s",
                     "body": "REAL", "content": "IMPOSTOR"})
        rec = _staged_record(out["staged_path"])
        assert rec["body"] == "REAL", f"alias clobbered explicit body: {rec['body']!r}"
        assert any("content" in n for n in out.get("unrecognized_arguments", [])), \
            "discarded duplicate was not reported"

        # --- Guard 3: a genuinely unknown argument is surfaced, never swallowed ---
        out = _call("brain_propose_node",
                    {**base, "title": "argnorm g3", "summary": "s", "totally_bogus": "xyz"})
        assert "totally_bogus" in out.get("unrecognized_arguments", []), \
            "unknown argument vanished without a warning"
        assert out.get("warning"), "unknown argument reported without a warning message"

        # --- Guard 4: missing required fields yield a contract, not a KeyError ---
        out = _call("brain_propose_node", {"node_type": "fact", "title": "x"})
        assert out.get("error") == "missing_required_arguments", out
        assert set(out["missing"]) == {"family", "summary", "asserted_by"}, out["missing"]
        assert "accepted" in out and "required" in out, \
            "error must publish the contract so the caller can self-correct"

        # --- Guard 5: brain_search accepts the natural synonyms for `text` ---
        for synonym in ("query", "topic", "q"):
            args, aliased, _ = M._normalize_args("brain_search", {synonym: "atlas", "limit": 1})
            assert args.get("text") == "atlas", f"brain_search rejected `{synonym}`"
            assert aliased, f"`{synonym}` alias applied silently"

        # --- Guard 6: REQUIRED_ARGS must not drift from the published schemas ---
        for tool in M.TOOLS:
            name = tool["name"]
            if name not in M.REQUIRED_ARGS:
                continue
            schema_required = set(tool["inputSchema"].get("required", []))
            assert set(M.REQUIRED_ARGS[name]) == schema_required, (
                f"{name}: REQUIRED_ARGS {M.REQUIRED_ARGS[name]} drifted from "
                f"inputSchema.required {sorted(schema_required)}")
            # every alias target must be a real field on that tool
            known = M._known_fields(name)
            for src, dst in M.ARG_ALIASES.get(name, {}).items():
                assert dst in known, f"{name}: alias {src} → {dst} targets no real field"
    finally:
        M.STAGING_DIR = orig_staging
        tmp.cleanup()

    print("test_mcp_arg_normalization: PASS (6 guards)")
    return 0


if __name__ == "__main__":
    sys.exit(run())
